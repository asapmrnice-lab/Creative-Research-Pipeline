"""The extraction stage: posts in, Structured Fields out.

What this module is careful about, in the order it matters:

**It never trims a post to fit.** Plan §5 forbids silent truncation, and the
reason is specific: the value most likely to fall off the end of a cut post is
the KPI in the last line. A long post is split on paragraph boundaries and
every chunk is extracted, then the results are merged -- so a long post costs
more, and loses nothing.

**It writes through `MachineStore`, not `Store`.** The object it is handed has
no `add_note` on it. Extraction physically cannot file a fact as analysis.

**It is re-runnable.** Every task is keyed by `(item_id, chunk, prompt_version)`
so re-running after a failure repeats work rather than corrupting it, and the
engine reports what it did in numbers that add up.

**A skipped model is not a failure.** With `DisabledLLM` wired in, every item
comes back `skipped`, nothing is written, and the run reports honestly that no
extraction happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ..llm.protocol import JsonResult, JsonTask, LLMClient, Provenance, Usage
from . import whitelist as wl

# Roughly a page and a half of text. Chosen well below any model limit: the
# point is not to fit the context window, it is that a short prompt about a
# short passage extracts more reliably than a long one about a long passage.
DEFAULT_CHUNK_CHARS = 6000

# Extraction returns a handful of short strings. A tight ceiling keeps a
# runaway response cheap; the parse failure it would cause is reported, not
# silently stored as a partial fact.
DEFAULT_MAX_TOKENS = 1024

_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")
_PARAGRAPH = re.compile(r"\n\s*\n")


def _key(item_id: int, chunk: int, version: str) -> str:
    """`(item_id, prompt_version)` from plan §5, plus the chunk that splits it."""
    return f"x{item_id}-c{chunk}-{_UNSAFE_KEY.sub('-', version)}"[:64]


def chunk_text(text: str, limit: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Split on blank lines, never mid-sentence, never dropping anything.

    Paragraphs are packed greedily so a post that fits stays a single call. A
    single paragraph longer than the limit is left whole rather than cut --
    losing a sentence boundary to save tokens would be exactly the silent
    truncation the plan rules out.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= limit:
            current = f"{current}\n\n{paragraph}"
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


@dataclass
class ExtractionOutcome:
    """What happened to one item."""

    item_id: int
    fields: tuple[tuple[str, str], ...] = ()
    confidence: float | None = None
    error: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


@dataclass
class ExtractionReport:
    """A run, in numbers that reconcile against the items it was given."""

    seen: int = 0
    extracted: int = 0
    skipped: int = 0
    failed: int = 0
    fields_written: int = 0
    usage: Usage = field(default_factory=Usage)

    def check(self) -> None:
        assert self.seen == self.extracted + self.skipped + self.failed


class ExtractionEngine:
    """Turns posts into whitelisted facts. Holds no state between runs."""

    def __init__(
        self,
        client: LLMClient,
        *,
        whitelist: wl.Whitelist | None = None,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._whitelist = whitelist or wl.default()
        self._chunk_chars = chunk_chars
        self._max_tokens = max_tokens
        # Built once. These are the cacheable prefix, so they must be byte-
        # identical across every call in a run or nothing caches.
        self._instructions = self._whitelist.instructions()
        self._schema = self._whitelist.schema()

    @property
    def version(self) -> str:
        return self._whitelist.version

    def provenance(self, confidence: float | None = None) -> Provenance:
        return Provenance(
            model=self._client.model,
            prompt_version=self._whitelist.version,
            confidence=confidence,
        )

    # -- extract ------------------------------------------------------------

    def extract(self, items: Sequence[tuple[int, str | None]]) -> list[ExtractionOutcome]:
        """One outcome per item, in the order given. Writes nothing."""
        tasks: list[JsonTask] = []
        owner: list[int] = []
        for item_id, text in items:
            for index, chunk in enumerate(chunk_text(text or "", self._chunk_chars)):
                tasks.append(
                    JsonTask(
                        key=_key(item_id, index, self._whitelist.version),
                        instructions=self._instructions,
                        payload=chunk,
                        schema=self._schema,
                        max_tokens=self._max_tokens,
                    )
                )
                owner.append(item_id)

        results = self._client.complete_json(tasks) if tasks else []
        by_item: dict[int, list[JsonResult]] = {item_id: [] for item_id, _ in items}
        for item_id, result in zip(owner, results):
            by_item[item_id].append(result)

        return [self._merge(item_id, by_item[item_id]) for item_id, _ in items]

    def _merge(self, item_id: int, results: list[JsonResult]) -> ExtractionOutcome:
        """Combine a post's chunks into one outcome.

        Values are unioned with their order preserved, because a fact stated in
        the third paragraph is as true as one in the first. Confidence takes
        the minimum: the least certain chunk is the reason to look at the post.
        """
        if not results:
            # No text to extract from. Not an error and not a model skip.
            return ExtractionOutcome(item_id=item_id, skipped=True)

        errors = [r.error for r in results if r.error]
        if errors:
            return ExtractionOutcome(item_id=item_id, error="; ".join(errors))
        if all(r.skipped for r in results):
            return ExtractionOutcome(item_id=item_id, skipped=True)

        pairs: list[tuple[str, str]] = []
        confidences: list[float] = []
        for result in results:
            if not result.data:
                continue
            for pair in self._whitelist.values(result.data):
                if pair not in pairs:
                    pairs.append(pair)
            reported = self._whitelist.confidence(result.data)
            if reported is not None:
                confidences.append(reported)

        return ExtractionOutcome(
            item_id=item_id,
            fields=tuple(pairs),
            confidence=min(confidences) if confidences else None,
        )

    # -- extract and store --------------------------------------------------

    def run(self, items: Sequence[tuple[int, str | None]], store) -> ExtractionReport:
        """Extract and write. `store` must satisfy `MachineStore`.

        The store is typed by the protocol rather than the class, which is what
        keeps this stage unable to write a Note no matter what it is handed.
        """
        report = ExtractionReport(seen=len(items))
        for outcome in self.extract(items):
            if outcome.error:
                report.failed += 1
                continue
            if outcome.skipped:
                report.skipped += 1
                continue

            report.extracted += 1
            provenance = self.provenance(outcome.confidence)
            for name, value in outcome.fields:
                store.add_machine_field(outcome.item_id, name, value, provenance)
                report.fields_written += 1

        report.check()
        return report
