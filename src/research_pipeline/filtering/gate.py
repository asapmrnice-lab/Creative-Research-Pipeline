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
from .scope import ChannelScope


class Verdict(str, Enum):
    COLLECT = "collect"
    OUT_OF_SCOPE = "out-of-scope"
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
    """Collects a post only when it is in scope AND carries a configured keyword.

    Two independent rules, both the human's to set: which channels are being
    read (ChannelScope) and which words are worth keeping (KeywordFilter). The
    gate holds them together because the pipeline needs one place to ask, but
    neither rule knows about the other.

    Rejection is silent by design -- a rejected post is not stored anywhere,
    not even as a tombstone. The architecture's "never deletes" guarantee is
    about collected items; a post that was never collected was never an item.
    """

    def __init__(
        self, keyword_filter: KeywordFilter, scope: ChannelScope | None = None
    ) -> None:
        self._filter = keyword_filter
        # No scope means every channel the source yields, which is what every
        # caller written before this parameter existed already expects.
        self._scope = scope if scope is not None else ChannelScope()

    @property
    def keywords(self) -> tuple[str, ...]:
        """The list being enforced, for callers that want to report it."""
        return self._filter.config.keywords

    @property
    def scope(self) -> ChannelScope:
        """The channel scope being enforced, for callers that want to report it."""
        return self._scope

    def evaluate(self, post: RawPost) -> Decision:
        # Scope is checked first, and not merely because it is the cheapest
        # test. A post from a channel we are not collecting was never a
        # candidate, so filing it under no-keyword would mix it in with posts
        # from tracked channels that genuinely missed -- and that second number
        # is the one that tells the human whether the keyword list is too
        # narrow. Keeping them apart is what makes it readable.
        if not self._scope.allows(post.source):
            return Decision(Verdict.OUT_OF_SCOPE)

        # A media-only post has nothing to match against. It is rejected for a
        # different reason than a post that was read and found wanting, and
        # the counters keep them apart so the numbers stay honest.
        if not post.text or not post.text.strip():
            return Decision(Verdict.NO_TEXT)

        keywords = tuple(self._filter.matched_keywords(post.text))
        if not keywords:
            return Decision(Verdict.NO_KEYWORD)

        return Decision(Verdict.COLLECT, keywords)
