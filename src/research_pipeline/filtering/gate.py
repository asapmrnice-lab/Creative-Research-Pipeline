"""The collection gate: the one place that decides if a post is collected.

Until now KeywordFilter was only ever used to *report* on posts that had
already been downloaded and stored. This module is what makes it a gate --
the pipeline asks it before anything is written, and a post it rejects is
never stored, so its media is never fetched either.

The gate answers with a reason, not just a boolean, so a run can explain
itself and a rejection can be audited without re-running the filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..domain import RawPost
from .keywords import KeywordFilter


class Verdict(str, Enum):
    COLLECT = "collect"
    NO_TEXT = "no-text"
    NO_KEYWORD = "no-keyword"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    keywords: tuple[str, ...] = ()

    @property
    def collect(self) -> bool:
        return self.verdict is Verdict.COLLECT


class KeywordGate:
    """Collects a post only when its text carries a configured keyword.

    Rejection is silent by design -- a rejected post is not stored anywhere,
    not even as a tombstone. The architecture's "never deletes" guarantee is
    about collected items; a post that was never collected was never an item.
    """

    def __init__(self, keyword_filter: KeywordFilter) -> None:
        self._filter = keyword_filter

    @property
    def keywords(self) -> tuple[str, ...]:
        """The list being enforced, for callers that want to report it."""
        return self._filter.config.keywords

    def evaluate(self, post: RawPost) -> Decision:
        # A media-only post has nothing to match against. It is rejected for a
        # different reason than a post that was read and found wanting, and
        # the counters keep them apart so the numbers stay honest.
        if not post.text or not post.text.strip():
            return Decision(Verdict.NO_TEXT)

        keywords = tuple(self._filter.matched_keywords(post.text))
        if not keywords:
            return Decision(Verdict.NO_KEYWORD)

        return Decision(Verdict.COLLECT, keywords)
