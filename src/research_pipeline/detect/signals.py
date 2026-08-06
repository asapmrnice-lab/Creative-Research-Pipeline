"""Language and format detection: plan §3, the fourth allowed row.

The plan lists this as something the model may do. It does not have to: which
script a post is written in, and whether it contains a link or a table, are
countable properties of the characters. Counting them is free, instant, and
cannot hallucinate -- so this stage never calls a model, and the row's real
value turns out to be the line beside it in the Forbidden column.

That line is *"summarizing so the human doesn't have to read it."* The
difference is the whole design: a label says what a post **is**, so the human
can find it later. A summary says what a post **means**, so the human can skip
it. Everything here is the first kind. There is deliberately no `topic`, no
`quality`, and no `category` -- those would be judgments wearing a label's
clothing.

Output goes to Structured Fields like any other mechanical fact, with
`origin='system'` and the provenance of this module rather than of a model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Bump when a rule changes: the label is stored with its version, so an old
# label stays attributable to the rule that produced it.
DETECT_VERSION = "detect-1"

LANG_RU = "ru"
LANG_EN = "en"
LANG_MIXED = "mixed"
LANG_UNKNOWN = "unknown"

# One script has to be clearly ahead to claim the post; otherwise it is mixed.
# Ad copy routinely carries a Latin brand name inside Russian prose, and
# calling that post English would be wrong in a way that hides it from search.
_DOMINANT = 0.75

# Below this there is not enough text to be reading a trend off it.
_MIN_LETTERS = 8

_URL = re.compile(r"https?://\S+|(?:^|\s)(?:t\.me|www\.)/?\S+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z][\w]{3,}")
_HASHTAG = re.compile(r"(?<![\w#])#[^\s#]+")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")

# A markdown-ish table needs a row of pipes; two pipes on one line is the
# cheapest honest signal, and a separator row confirms it.
_TABLE_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?[\s:-]*-{2,}[\s:|-]*$", re.MULTILINE)

_BULLET = re.compile(r"^\s*(?:[-*•‣▪—–]|\d{1,2}[.)])\s+\S", re.MULTILINE)

# Digits with a currency mark or a unit-of-thousands word beside them. Both
# orders, because the symbol leads in "$5000" and trails in "5000 руб" -- and
# a post mixing English and Russian ad copy routinely contains both.
_AMOUNT = re.compile(
    r"[$€₽£]\s*\d"
    r"|\d[\d\s.,]*\s*(?:[$€₽£]|руб|usd|eur|k\b|кк?\b|тыс|млн)",
    re.IGNORECASE,
)


def _emoji_count(text: str) -> int:
    """Characters Unicode classes as symbols or unassigned-but-printable.

    `unicodedata` has no "is emoji" predicate, and hardcoding codepoint ranges
    would go stale with every Unicode release. Category `So` (Symbol, other)
    covers the pictographs without needing a table to maintain.
    """
    return sum(1 for char in text if unicodedata.category(char) == "So")


@dataclass(frozen=True)
class Signals:
    """What the text is, in labels a human can verify by looking."""

    language: str
    has_link: bool = False
    has_table: bool = False
    has_list: bool = False
    has_mention: bool = False
    has_hashtag: bool = False
    has_amount: bool = False
    emoji_count: int = 0
    letters: int = 0

    @property
    def version(self) -> str:
        return DETECT_VERSION

    def as_fields(self) -> tuple[tuple[str, str], ...]:
        """Flatten to (name, value) pairs for the Structured Field table.

        Only the true flags are emitted. A row saying `has_table = false` is
        not an observation about the post, it is an observation about the
        schema, and the store holds the former.
        """
        fields: list[tuple[str, str]] = [("language", self.language)]
        for name, present in (
            ("has_link", self.has_link),
            ("has_table", self.has_table),
            ("has_list", self.has_list),
            ("has_mention", self.has_mention),
            ("has_hashtag", self.has_hashtag),
            ("has_amount", self.has_amount),
        ):
            if present:
                fields.append((name, "true"))
        if self.emoji_count:
            fields.append(("emoji_count", str(self.emoji_count)))
        return tuple(fields)


def _language(text: str) -> tuple[str, int]:
    """Which script the *prose* is in.

    URLs, @handles and hashtags are removed before counting. They are Latin
    almost by definition -- a domain name has to be -- so leaving them in makes
    a Russian post carrying two links look bilingual. That is not a cosmetic
    error: it puts the post under the wrong label, and the label exists to find
    the post again.
    """
    text = _HASHTAG.sub(" ", _MENTION.sub(" ", _URL.sub(" ", text)))

    cyrillic = latin = 0
    for char in text:
        if not char.isalpha():
            continue
        # Name lookup rather than a codepoint range: it reads as the question
        # being asked, and covers the accented and extended letters too.
        try:
            name = unicodedata.name(char)
        except ValueError:  # pragma: no cover - unnamed codepoint
            continue
        if "CYRILLIC" in name:
            cyrillic += 1
        elif "LATIN" in name:
            latin += 1

    letters = cyrillic + latin
    if letters < _MIN_LETTERS:
        return LANG_UNKNOWN, letters
    if cyrillic / letters >= _DOMINANT:
        return LANG_RU, letters
    if latin / letters >= _DOMINANT:
        return LANG_EN, letters
    return LANG_MIXED, letters


def detect(text: str | None) -> Signals:
    """Label one post. Deterministic, and the same answer every time."""
    if not text or not text.strip():
        return Signals(language=LANG_UNKNOWN)

    language, letters = _language(text)
    return Signals(
        language=language,
        has_link=bool(_URL.search(text)),
        has_table=bool(_TABLE_RULE.search(text)) and bool(_TABLE_ROW.search(text)),
        has_list=bool(_BULLET.search(text)),
        # An email is not a mention, and both use @.
        has_mention=bool(_MENTION.search(_EMAIL.sub(" ", text))),
        has_hashtag=bool(_HASHTAG.search(text)),
        has_amount=bool(_AMOUNT.search(text)),
        emoji_count=_emoji_count(text),
        letters=letters,
    )
