"""Near-duplicate detection: plan §7 tier 2. No model.

Tier 1 already exists -- `RawPost.content_hash()` catches a repost that is
byte-identical after whitespace folding. This is the tier that catches the
repost with an emoji added or a line reworded: SimHash gives near-identical
texts near-identical fingerprints, so similarity becomes a bit count instead
of a comparison of every post against every other post.

Two properties this must have, and both are tested:

**Stable across processes.** Python's built-in `hash()` is randomised per
interpreter run, so a fingerprint computed today would not match the same text
tomorrow and stored values would be worthless. Every hash here comes from
`hashlib`.

**Flag, never delete.** The architecture guarantees collected items stay
searchable forever, so this reports pairs and stops. Nothing in this module
writes to the store, and no caller of it merges anything -- the human decides,
in review, whether two flagged posts are the same observation.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..filtering.keywords import normalize

BITS = 64
_MASK = (1 << BITS) - 1

# Word-ish runs, Cyrillic included. Punctuation carries no signal about
# whether two posts say the same thing.
_TOKEN = re.compile(r"\w+", re.UNICODE)

# Two adjacent words per feature. Single words lose all word order, so
# "creative about cases" and "cases about creative" would collide; pairs keep
# enough order to tell them apart while surviving an inserted emoji.
SHINGLE = 2

# Measured, not guessed. Over the 65 real posts in tests/fixtures, four kinds
# of repost edit (reflowed, footer appended, repost header prepended,
# punctuation stripped) never exceeded 9 bits, and no unrelated pair came
# closer than 17. 12 sits in that gap with margin on both sides: it caught
# every repost and flagged no unrelated pair. See test_machine_stages.py,
# which re-derives this from the corpus so a regression fails the suite.
DEFAULT_DISTANCE = 12


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def _features(text: str) -> list[str]:
    words = _tokens(text)
    if len(words) < SHINGLE:
        return words
    return [" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)]


def simhash(text: str | None) -> int:
    """A 64-bit fingerprint where similar texts differ in few bits."""
    features = _features(text or "")
    if not features:
        return 0

    # One counter per bit: every feature votes its own hash bits up or down,
    # and the sign of each column becomes that bit of the fingerprint. A
    # feature that appears in both texts cancels out of the difference.
    columns = [0] * BITS
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(BITS):
            columns[bit] += 1 if value >> bit & 1 else -1

    fingerprint = 0
    for bit, weight in enumerate(columns):
        if weight > 0:
            fingerprint |= 1 << bit
    return fingerprint


def simhash_hex(text: str | None) -> str:
    """The fingerprint as stored -- 16 hex chars, so SQLite keeps it exact.

    SQLite INTEGER is signed 64-bit and a fingerprint uses the full unsigned
    range, so storing it as a number would wrap the top bit. Text does not.
    """
    return f"{simhash(text):016x}"


def hamming(left: int, right: int) -> int:
    """How many bits differ. Fewer means more similar."""
    return ((left ^ right) & _MASK).bit_count()


@dataclass(frozen=True)
class DedupConfig:
    """How close counts as a near-duplicate. The human's threshold, not ours."""

    distance: int = DEFAULT_DISTANCE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DedupConfig":
        env = os.environ if env is None else env
        raw = (env.get("DEDUP_SIMHASH_DISTANCE") or "").strip()
        if not raw:
            return cls()
        try:
            distance = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"DEDUP_SIMHASH_DISTANCE must be a whole number of bits, got {raw!r}"
            ) from exc
        if not 0 <= distance <= BITS:
            raise ValueError(
                f"DEDUP_SIMHASH_DISTANCE must be within 0..{BITS}, got {distance}"
            )
        return cls(distance=distance)


@dataclass(frozen=True)
class NearDuplicate:
    """A pair worth a human's attention. Not a verdict."""

    item_id: int
    duplicate_of: int
    distance: int


class NearDuplicateIndex:
    """Accumulates fingerprints and reports which items look alike.

    Deliberately a plain scan. The store holds thousands of posts, not
    millions, and 64-bit XOR over a few thousand rows is instant -- a banded
    LSH index would be faster in principle and a source of subtle bugs in
    practice, which is a bad trade at this size.
    """

    def __init__(self, config: DedupConfig | None = None) -> None:
        self._config = config or DedupConfig()
        self._seen: list[tuple[int, int]] = []

    @property
    def distance(self) -> int:
        return self._config.distance

    def add(self, item_id: int, text: str | None) -> NearDuplicate | None:
        """Register an item, returning the closest earlier match if any."""
        return self.add_fingerprint(item_id, simhash(text))

    def add_fingerprint(self, item_id: int, fingerprint: int) -> NearDuplicate | None:
        best: NearDuplicate | None = None
        for seen_id, seen_fingerprint in self._seen:
            gap = hamming(fingerprint, seen_fingerprint)
            if gap <= self._config.distance and (best is None or gap < best.distance):
                best = NearDuplicate(item_id=item_id, duplicate_of=seen_id, distance=gap)
        self._seen.append((item_id, fingerprint))
        return best

    def scan(self, items: Iterable[tuple[int, str | None]]) -> list[NearDuplicate]:
        """Fingerprint a whole store and report every flagged pair."""
        return [flag for item_id, text in items if (flag := self.add(item_id, text))]
