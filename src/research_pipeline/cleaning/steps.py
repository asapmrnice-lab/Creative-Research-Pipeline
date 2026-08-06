"""Deterministic cleaning: plan §8, stage 1.

Every step here is a rule a human could apply by hand and check by reading, so
none of them needs a model and none of them can be wrong in an interesting
way. That matters beyond cost: these steps run whether or not the model stage
is enabled, which is what plan §3's fallback ("regex/heuristic noise-cleaning
with no model at all") actually amounts to.

The division of labour between code and config is deliberate:

  * The universal steps -- unicode form, invisible characters, whitespace,
    tracking parameters -- are *algorithms*. They are the same for every
    channel, so they live in code.
  * The channel's own boilerplate -- its subscribe footer, its ad block -- is a
    *value*. It differs per source and the human decides it, so it lives in
    .env and nothing here knows a single one of those strings.

Every step returns what it removed. A cleaner that quietly deleted a sentence
would be indistinguishable from one that worked, so removals are recorded and
carried to the store alongside the cleaned text.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# This prompt version travels with the output, so a change here must be a
# change to the string. Bump it whenever a step's behaviour changes.
CLEAN_VERSION = "clean-det-1"

# Zero-width and bidi-control characters. They survive copy-paste, break
# keyword matching, and are invisible to the human deciding whether a post
# matched -- which makes them exactly the noise this stage exists to remove.
#
# U+200D ZERO WIDTH JOINER is deliberately NOT in this class. In these posts it
# is load-bearing: it is what binds "👩‍❤️‍👨" into one emoji, and stripping it
# blindly silently rewrites that into three unrelated ones. It is handled
# separately below.
_INVISIBLE = re.compile(
    "["
    "​‌"  # zero-width space, non-joiner
    "‎‏"  # LRM / RLM
    "‪-‮"  # bidi embedding / override
    "⁠-⁤"  # word joiner, invisible operators
    "﻿"  # BOM
    "]"
)

# Emoji, dingbats, and the variation selector that often precedes a joiner.
_EMOJI_ISH = "\U0001f000-\U0001faff☀-➿←-⇿️⃣"

# A joiner is noise only when it is *not* joining two emoji -- a stray one
# pasted into prose. Between emoji it is part of the character and stays.
_STRAY_ZWJ = re.compile(
    f"(?<![{_EMOJI_ISH}])‍|‍(?![{_EMOJI_ISH}])"
)

# Control characters other than tab and newline, which carry no text.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACE_RUN = re.compile(r"[ \t]{2,}")

_URL = re.compile(r"https?://[^\s<>()\[\]{}«»\"']+")

# Analytics parameters: they identify the click, never the content.
_TRACKING_PARAMS = ("fbclid", "gclid", "yclid", "igshid", "mc_cid", "mc_eid", "_openstat")

# Parameters a redirector hides the real destination in.
_REDIRECT_PARAMS = ("u", "url", "to", "target", "redirect", "link")


@dataclass(frozen=True)
class Removal:
    """One thing a step took out, and which step took it.

    Kept so the human can audit the cleaner rather than trust it.
    """

    step: str
    text: str


@dataclass(frozen=True)
class CleaningResult:
    text: str
    removals: tuple[Removal, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.removals)


@runtime_checkable
class CleaningStep(Protocol):
    """One rule. New cleaning rule = new step, no edit to the pipeline."""

    @property
    def name(self) -> str:
        ...

    def apply(self, text: str) -> tuple[str, list[str]]:
        """Return the cleaned text and whatever was removed."""
        ...


# -- the universal steps ----------------------------------------------------


class NormalizeUnicode:
    """NFKC, so look-alike forms compare equal.

    The same fold the keyword filter already applies before matching. Doing it
    here too means the stored cleaned text agrees with the text the gate
    judged.
    """

    name = "normalize-unicode"

    def apply(self, text: str) -> tuple[str, list[str]]:
        folded = unicodedata.normalize("NFKC", text)
        return folded, [] if folded == text else ["<unicode normalized>"]


class StripInvisible:
    """Remove zero-width, bidi and control characters.

    Leaves the joiners that hold a multi-part emoji together. Removing those
    would not be cleaning -- it would be editing a character into different
    characters, which is the thing this whole stage is not allowed to do.
    """

    name = "strip-invisible"

    def apply(self, text: str) -> tuple[str, list[str]]:
        removed = (
            _INVISIBLE.findall(text) + _CONTROL.findall(text) + _STRAY_ZWJ.findall(text)
        )
        if not removed:
            return text, []
        cleaned = _STRAY_ZWJ.sub("", _CONTROL.sub("", _INVISIBLE.sub("", text)))
        return cleaned, [f"<{len(removed)} invisible characters>"]


class DropPatterns:
    """Remove the channel's own boilerplate, by the human's patterns.

    The patterns come from configuration and never from this module. A footer
    the machine decided to remove on its own would be the machine deciding
    what is worth keeping, which is the line plan §3 draws.
    """

    name = "drop-patterns"

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns: tuple[re.Pattern[str], ...] = tuple(
            re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns
        )

    def apply(self, text: str) -> tuple[str, list[str]]:
        removed: list[str] = []
        for pattern in self._patterns:
            matches = [m.group(0) for m in pattern.finditer(text)]
            if matches:
                removed.extend(m.strip() for m in matches if m.strip())
                text = pattern.sub("", text)
        return text, removed


class UnwrapTrackingUrls:
    """Restore the real destination of a link, and drop click identifiers.

    A redirect wrapper hides where a link goes, which defeats searching for it
    later; a tracking parameter makes two identical links look different,
    which defeats dedup. Both are noise about the click rather than content.
    """

    name = "unwrap-urls"

    def apply(self, text: str) -> tuple[str, list[str]]:
        removed: list[str] = []

        def rewrite(match: re.Match[str]) -> str:
            original = match.group(0)
            cleaned = self._clean_url(original)
            if cleaned != original:
                removed.append(original)
            return cleaned

        return _URL.sub(rewrite, text), removed

    def _clean_url(self, url: str) -> str:
        parts = urlsplit(url)
        if not parts.query:
            return url
        params = parse_qsl(parts.query, keep_blank_values=True)

        for key, value in params:
            if key.lower() in _REDIRECT_PARAMS and value.startswith(("http://", "https://")):
                # A wrapper around a real link: keep the destination, and clean
                # that too -- wrappers nest.
                return self._clean_url(value)

        kept = [
            (key, value)
            for key, value in params
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
        ]
        if len(kept) == len(params):
            return url
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
        )


class CollapseWhitespace:
    """Trailing spaces, doubled spaces, and runs of blank lines.

    Runs last: earlier steps leave holes where they removed things, and this
    is what closes them.
    """

    name = "collapse-whitespace"

    def apply(self, text: str) -> tuple[str, list[str]]:
        cleaned = _TRAILING_SPACE.sub("", text)
        cleaned = _SPACE_RUN.sub(" ", cleaned)
        cleaned = _BLANK_RUN.sub("\n\n", cleaned).strip()
        return cleaned, [] if cleaned == text else ["<whitespace collapsed>"]


# -- configuration ----------------------------------------------------------


@dataclass(frozen=True)
class CleaningConfig:
    """The human's cleaning rules. No pattern is written into the code."""

    drop_patterns: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CleaningConfig":
        """Read `CLEAN_DROP_PATTERNS`, a JSON array of regexes.

        JSON rather than a comma-separated list because a regex may contain a
        comma -- splitting on one would silently cut patterns in half and the
        halves would still compile.
        """
        env = os.environ if env is None else env
        raw = (env.get("CLEAN_DROP_PATTERNS") or "").strip()
        if not raw:
            return cls()
        try:
            patterns = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"CLEAN_DROP_PATTERNS must be a JSON array of regexes: {exc}"
            ) from exc
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError("CLEAN_DROP_PATTERNS must be a JSON array of strings")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"CLEAN_DROP_PATTERNS contains an invalid regex {pattern!r}: {exc}"
                ) from exc
        return cls(drop_patterns=tuple(patterns))


class DeterministicCleaner:
    """Runs the steps in order. Idempotent: cleaning twice changes nothing."""

    def __init__(self, config: CleaningConfig | None = None) -> None:
        config = config or CleaningConfig()
        self.version = CLEAN_VERSION
        self._steps: tuple[CleaningStep, ...] = (
            NormalizeUnicode(),
            StripInvisible(),
            DropPatterns(config.drop_patterns),
            UnwrapTrackingUrls(),
            CollapseWhitespace(),
        )

    @property
    def steps(self) -> tuple[str, ...]:
        return tuple(step.name for step in self._steps)

    def clean(self, text: str | None) -> CleaningResult:
        if not text:
            return CleaningResult(text="")
        current = text
        removals: list[Removal] = []
        for step in self._steps:
            current, removed = step.apply(current)
            removals.extend(Removal(step.name, item) for item in removed)
        return CleaningResult(text=current, removals=tuple(removals))
