"""Model-assisted cleaning: plan §8, stage 2.

This is the least mechanical thing in the Allowed column and therefore the one
needing the most restraint. The instruction *"remove promotional boilerplate;
do not rephrase, summarise, or remove factual content"* is a request, and a
request is not a guarantee. So the output is verified before it is believed.

**The deletion-only check.** A cleaned text is accepted only if its whitespace-
delimited tokens form a subsequence of the original's. That is exactly the
claim "you only deleted whole spans": every surviving word was in the post,
unaltered, in the order it was in the post. A model that rephrased a sentence,
translated a word, corrected a typo, summarised a paragraph or edited a digit
out of a metric fails this by construction, and its output is discarded in
favour of the deterministic text. The guarantee costs one linear scan and does
not depend on the model cooperating.

That check is what makes the difference between "cleaning" and "rewriting"
enforceable rather than aspirational -- and it is the reason this stage can be
enabled without weakening plan §3's Core Principle. What it cannot check is
*which* spans were dropped; that is what `removals` is recorded for, and why
raw text is kept forever.

If the model is disabled, or fails, or fails the check, the deterministic
result stands. There is no configuration in which this stage can make the
stored text worse than the stage before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..llm.protocol import JsonResult, JsonTask, LLMClient, Provenance
from .steps import CleaningResult, DeterministicCleaner, Removal

# Bump on any change to the instructions or the schema below.
CLEAN_LLM_VERSION = "clean-llm-1"

DEFAULT_MAX_TOKENS = 4096

_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")

INSTRUCTIONS = """\
You remove promotional boilerplate from a marketing post. You are a redactor, \
not an editor.

You may delete only these:
- subscribe/join prompts and links to the channel itself
- the channel's signature, footer or header banner
- advertisements for the channel or its own paid products
- decorative separator lines and runs of decorative emoji

You must never:
- rephrase, reword, translate, correct, shorten or summarise anything
- delete any sentence that states a fact, a metric, a price, a date, a brand, \
a geo or a result
- delete a link that is part of what the post is reporting on
- add any word that is not already in the post

The text you return must be the original text with whole spans deleted and \
nothing else changed. Every character you return must appear in the original, \
in the same order. If you are unsure whether a span is boilerplate, keep it.

Return the cleaned text, and the list of spans you deleted, copied exactly."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cleaned_text", "removed"],
    "properties": {
        "cleaned_text": {
            "type": "string",
            "description": "The post with boilerplate spans deleted, nothing else changed.",
        },
        "removed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each deleted span, copied exactly from the original.",
        },
    },
}


def _key(item_id: int, version: str) -> str:
    return f"c{item_id}-{_UNSAFE_KEY.sub('-', version)}"[:64]


def is_deletion_only(original: str, cleaned: str) -> bool:
    """True when `cleaned` could have been produced by deleting whole tokens.

    Compared token by token, not character by character, and that distinction
    is the whole guarantee. At character granularity, turning "ROI 140%" into
    "ROI 14%" is a deletion -- one digit removed -- and would be accepted. It
    is obviously not a redaction; it is a falsified metric, and it is exactly
    the failure this check exists to catch.

    Tokens are whitespace-delimited runs, so every surviving token must appear
    in the original *whole* and in order. A model may drop a footer, a
    sentence or a paragraph; it may not reach inside a number, a price, a brand
    or a word. Whitespace itself is ignored, because closing the gap left by a
    removed span legitimately changes spacing.
    """
    source = original.split()
    target = cleaned.split()
    if len(target) > len(source):
        return False

    cursor = iter(source)
    return all(token in cursor for token in target)


@dataclass
class CleanOutcome:
    """One item's cleaning, and which stage's output ended up stored."""

    item_id: int
    text: str
    removals: tuple[Removal, ...] = ()
    used_model: bool = False
    rejected: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.removals)


class LlmCleaner:
    """Deterministic cleaning, then an optional model pass over the result.

    The model never sees the raw post: it sees what the deterministic steps
    produced, which is smaller, already normalised, and free of the invisible
    characters that would otherwise show up inside its returned spans.
    """

    def __init__(
        self,
        client: LLMClient,
        *,
        deterministic: DeterministicCleaner | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._client = client
        self._deterministic = deterministic or DeterministicCleaner()
        self._max_tokens = max_tokens

    @property
    def version(self) -> str:
        """The version this cleaner will actually produce.

        It names the passes that will *run*, not the passes that exist. With no
        model configured only the deterministic pass runs, so claiming the
        combined version would store a version no run produced -- and the next
        run, asking for items lacking that version, would get all of them back
        and reprocess the whole store forever.

        The reverse is the useful half: enabling the model changes this string,
        so exactly the items that were only ever cleaned deterministically come
        back up for reprocessing, and no others.
        """
        if not self._client.enabled:
            return self._deterministic.version
        return f"{self._deterministic.version}+{CLEAN_LLM_VERSION}"

    def provenance(self, *, used_model: bool) -> Provenance:
        """Which pipeline ran, and whether a model actually contributed.

        The two fields answer different questions and are set independently.
        `prompt_version` is always the version that ran, so an item is marked
        done and a re-run does not pay for it again -- including the item whose
        model output was rejected, which would otherwise be retried on every
        run forever at full price. `model` names the model only when its output
        survived the deletion-only check; text no model touched is attributed
        to the code that produced it, because recording a model's name on it
        would be a lie in exactly the record that exists to be checked.

        To genuinely reprocess, bump a version. That is the deliberate act.
        """
        return Provenance(
            model=self._client.model if used_model else "deterministic-cleaner",
            prompt_version=self.version,
        )

    def clean(self, items: Sequence[tuple[int, str | None]]) -> list[CleanOutcome]:
        first: dict[int, CleaningResult] = {
            item_id: self._deterministic.clean(text) for item_id, text in items
        }

        tasks = [
            JsonTask(
                key=_key(item_id, self.version),
                instructions=INSTRUCTIONS,
                payload=first[item_id].text,
                schema=SCHEMA,
                max_tokens=self._max_tokens,
            )
            for item_id, _ in items
            if first[item_id].text
        ]
        results = self._client.complete_json(tasks) if tasks else []
        by_key = {result.key: result for result in results}

        return [
            self._resolve(item_id, first[item_id], by_key.get(_key(item_id, self.version)))
            for item_id, _ in items
        ]

    def _resolve(
        self, item_id: int, base: CleaningResult, result: JsonResult | None
    ) -> CleanOutcome:
        fallback = CleanOutcome(item_id=item_id, text=base.text, removals=base.removals)

        if result is None or result.skipped:
            return fallback
        if result.error or not result.data:
            fallback.rejected = result.error or "empty response"
            return fallback

        cleaned = result.data.get("cleaned_text")
        if not isinstance(cleaned, str) or not cleaned.strip():
            fallback.rejected = "model returned no text"
            return fallback

        if not is_deletion_only(base.text, cleaned):
            # The model rewrote rather than redacted. Its output is discarded
            # whole -- there is no way to tell which part was rewritten, so
            # keeping any of it would keep an unknown amount of invention.
            fallback.rejected = "model altered the text rather than only deleting"
            return fallback

        removed = result.data.get("removed")
        spans = tuple(
            Removal(CLEAN_LLM_VERSION, str(span).strip())
            for span in (removed if isinstance(removed, list) else [])
            if str(span).strip()
        )
        return CleanOutcome(
            item_id=item_id,
            text=cleaned.strip(),
            removals=base.removals + spans,
            used_model=True,
        )

    def run(self, items: Sequence[tuple[int, str | None]], store) -> list[CleanOutcome]:
        """Clean and write. `store` must satisfy `MachineStore`.

        Writes to `cleaned_text`; the raw text is not an argument to anything
        here, and the store's trigger would refuse it anyway.
        """
        outcomes = self.clean(items)
        for outcome in outcomes:
            if outcome.text:
                store.set_cleaned_text(
                    outcome.item_id,
                    outcome.text,
                    self.provenance(used_model=outcome.used_model),
                )
        return outcomes
