"""Collection-time keyword filtering.

A post is ingested only when its text matches one of the configured keywords.
Everything is driven by environment config -- no keyword, threshold, or path is
written into this module.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, NamedTuple

# Inflectional endings for Russian masculine inanimate nouns (2nd declension).
# Applied to a keyword stem so that "креативы" / "креативом" still count as the
# same word. Longest first so the regex prefers the fullest ending it can match.
RU_NOUN_ENDINGS: tuple[str, ...] = (
    "ами",
    "ах",
    "ам",
    "ов",
    "ом",
    "а",
    "у",
    "е",
    "ы",
    "и",
)

MATCH_WHOLE_WORD = "whole-word"
MATCH_SUBSTRING = "substring"


class KeywordHit(NamedTuple):
    """One keyword occurrence found in a post."""

    keyword: str
    matched_text: str
    start: int
    end: int


def normalize(text: str) -> str:
    """Fold text to the form the matcher compares against.

    NFKC collapses look-alike Unicode forms, casefold handles capitalisation,
    and ё -> е removes the single most common Russian spelling split.
    """
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


@dataclass(frozen=True)
class KeywordFilterConfig:
    keywords: tuple[str, ...]
    match: str = MATCH_WHOLE_WORD
    allow_russian_inflections: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KeywordFilterConfig":
        env = os.environ if env is None else env
        raw = env.get("INGEST_KEYWORDS", "")
        keywords = tuple(k.strip() for k in raw.split(",") if k.strip())
        if not keywords:
            raise ValueError(
                "INGEST_KEYWORDS is empty -- collection-time filtering would "
                "discard every post. Set it in .env before running ingest."
            )

        match = env.get("INGEST_KEYWORD_MATCH", MATCH_WHOLE_WORD).strip()
        if match not in (MATCH_WHOLE_WORD, MATCH_SUBSTRING):
            raise ValueError(
                f"INGEST_KEYWORD_MATCH must be {MATCH_WHOLE_WORD!r} or "
                f"{MATCH_SUBSTRING!r}, got {match!r}"
            )

        inflections = env.get("INGEST_KEYWORD_INFLECTIONS", "true").strip().casefold()
        return cls(
            keywords=keywords,
            match=match,
            allow_russian_inflections=inflections in ("1", "true", "yes", "on"),
        )


class KeywordFilter:
    """Decides whether a post's text carries one of the configured keywords."""

    def __init__(self, config: KeywordFilterConfig) -> None:
        self.config = config
        self._patterns: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
            (keyword, self._compile(keyword)) for keyword in config.keywords
        )

    def _compile(self, keyword: str) -> re.Pattern[str]:
        stem = re.escape(normalize(keyword))
        if self.config.match == MATCH_SUBSTRING:
            return re.compile(stem)

        # Whole-word: the keyword may carry an inflectional ending, but nothing
        # else -- "креативы" matches, "креативный" and "прекреатив" do not.
        if self.config.allow_russian_inflections:
            endings = "|".join(RU_NOUN_ENDINGS)
            stem = f"{stem}(?:{endings})?"

        # Deliberately stricter than \b: a hyphen glued to the keyword makes a
        # compound word ("анти-кейс", "видео-креатив"), and a compound is not
        # the keyword. \b treats "-" as a separator, which would admit those
        # while still rejecting the unhyphenated "антикейс" -- an inconsistency
        # the keyword list should not have. Punctuation that genuinely leaves
        # the word standing alone ("#креатив", «кейс», "кейс/крео") still
        # matches. To collect a compound, add it to INGEST_KEYWORDS by name.
        return re.compile(rf"(?<![\w-]){stem}(?![\w-])")

    def find(self, text: str | None) -> list[KeywordHit]:
        """Return every keyword occurrence, with offsets into the normalized text."""
        if not text:
            return []
        haystack = normalize(text)
        hits: list[KeywordHit] = []
        for keyword, pattern in self._patterns:
            for m in pattern.finditer(haystack):
                hits.append(KeywordHit(keyword, m.group(0), m.start(), m.end()))
        hits.sort(key=lambda h: h.start)
        return hits

    def matches(self, text: str | None) -> bool:
        """True when the post should be collected."""
        if not text:
            return False
        haystack = normalize(text)
        return any(pattern.search(haystack) for _, pattern in self._patterns)

    def matched_keywords(self, text: str | None) -> list[str]:
        """The configured keywords present in the text, in config order."""
        return [k for k, _ in self._patterns if any(h.keyword == k for h in self.find(text))]


def filter_texts(texts: Iterable[str | None], kf: KeywordFilter) -> list[str]:
    """Convenience helper: keep only the texts that match."""
    return [t for t in texts if t is not None and kf.matches(t)]
