"""Check the keyword filter against real posts from the Telegram archive.

test_keywords.py uses invented strings to pin the matching rules. This module
uses text that actually appeared in the tracked channel, frozen into
tests/fixtures/real_posts.json by scripts/make_fixtures.py, so a rule that
passes in theory but fails on real posts is caught here.

Runs entirely offline -- the fixture is committed, no database required. The
tests at the bottom additionally re-verify the whole archive, and skip when
the archive is not on this machine.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from research_pipeline.env import load_project_env
from research_pipeline.filtering import KeywordFilter, KeywordFilterConfig
from research_pipeline.filtering.keywords import RU_NOUN_ENDINGS, normalize

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "real_posts.json").read_text(encoding="utf-8")
)


def build_filter() -> KeywordFilter:
    """The filter configured exactly as the fixture was generated."""
    cfg = FIXTURE["config"]
    return KeywordFilter(
        KeywordFilterConfig(
            keywords=tuple(cfg["keywords"]),
            match=cfg["match"],
            allow_russian_inflections=cfg["allow_russian_inflections"],
        )
    )


def ids(records: list[dict]) -> list[str]:
    return [f"msg{r['msg_id']}" for r in records]


# --------------------------------------------------------------------------
# Posts that carry a keyword must be collected
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "post", FIXTURE["matching"], ids=ids(FIXTURE["matching"])
)
def test_real_matching_post_is_collected(post):
    assert build_filter().matches(post["text"]), post["text"][:120]


@pytest.mark.parametrize(
    "post", FIXTURE["matching"], ids=ids(FIXTURE["matching"])
)
def test_real_matching_post_reports_the_right_keywords(post):
    """Not just 'it matched' -- it matched for the reason we expect."""
    kf = build_filter()
    assert kf.matched_keywords(post["text"]) == post["expect_keywords"]
    assert sorted({h.matched_text for h in kf.find(post["text"])}) == post[
        "expect_forms"
    ]


def covered_forms() -> set[str]:
    forms: set[str] = set()
    for post in FIXTURE["matching"]:
        forms |= set(post["expect_forms"])
    return forms


def test_fixture_covers_the_bare_form_of_every_keyword_present():
    """Guards the fixture itself: regenerating must not quietly lose coverage.

    "креативчик" genuinely never occurs in this archive -- that absence is
    asserted too, so if the channel starts using it the fixture is stale and
    this test says so.
    """
    covered = covered_forms()
    assert {k for k in FIXTURE["config"]["keywords"] if k in covered} == {
        "креатив",
        "крео",
        "креос",
        "кейс",
    }
    for keyword in FIXTURE["config"]["keywords"]:
        inflected = {f for f in covered if f.startswith(keyword)}
        if keyword == "креативчик":
            assert not inflected, "креативчик now occurs -- regenerate the fixture"
        else:
            assert keyword in inflected, f"no real post covers bare {keyword!r}"


# --------------------------------------------------------------------------
# Posts that carry no keyword must be rejected
#
# These are real posts the trial tool downloaded and stored anyway -- they are
# the reason collection-time filtering exists.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "post", FIXTURE["non_matching"], ids=ids(FIXTURE["non_matching"])
)
def test_real_keywordless_post_is_rejected(post):
    kf = build_filter()
    assert not kf.matches(post["text"]), post["text"][:120]
    assert kf.find(post["text"]) == []
    assert kf.matched_keywords(post["text"]) == []


def test_archive_holds_far_more_keywordless_posts_than_matching_ones():
    """The gap this filter closes, stated as a number.

    The trial archive ingested unconditionally: most of what it stored has no
    keyword at all. Collection-time filtering is what stops that.
    """
    corpus = FIXTURE["corpus"]
    assert corpus["posts_matching"] + corpus["posts_not_matching"] == corpus[
        "posts_with_text"
    ]
    assert corpus["posts_not_matching"] > corpus["posts_matching"]


# --------------------------------------------------------------------------
# Near-miss: keyword present, but inside a longer word
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "post", FIXTURE["near_miss"], ids=ids(FIXTURE["near_miss"])
)
def test_keyword_inside_a_longer_word_is_not_collected(post):
    """Real posts that must stay out. This is the decision, not a trade-off.

    "антикейс", "видео-креативов" and "кейса-мануала" all contain a keyword
    inside a longer word. A compound is a different word, so the list does not
    cover it. Two of these are arguably relevant posts -- that cost is
    accepted deliberately, because the alternative is the filter guessing
    which compounds mean the same thing, which is judgement.

    To collect one, name it in INGEST_KEYWORDS. Never loosen the boundary:
    that would also readmit "креативный", "кейсбук" and "рекреация".
    """
    strict = build_filter()
    assert not strict.matches(post["text"])

    loose = KeywordFilter(
        KeywordFilterConfig(
            keywords=tuple(FIXTURE["config"]["keywords"]), match="substring"
        )
    )
    assert loose.matches(post["text"])


# --------------------------------------------------------------------------
# Whole-archive verification (skipped when the archive is absent)
# --------------------------------------------------------------------------


def archive_path() -> Path | None:
    load_project_env(ROOT)
    raw = os.environ.get("TRIAL_DB_PATH")
    if not raw:
        return None
    path = (ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw)
    return path if path.exists() else None


requires_archive = pytest.mark.skipif(
    archive_path() is None, reason="trial archive not present on this machine"
)


def read_archive_texts() -> list[str]:
    path = archive_path()
    assert path is not None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT text FROM messages WHERE text IS NOT NULL AND text != ''"
            )
        ]
    finally:
        conn.close()


@requires_archive
def test_every_match_in_the_archive_is_a_real_inflection():
    """No false positives across the full corpus.

    Each hit must be a configured keyword, optionally plus one Russian noun
    ending -- never a longer word that merely starts the same way.
    """
    kf = build_filter()
    allowed = {
        normalize(k) + e
        for k in FIXTURE["config"]["keywords"]
        for e in ("",) + RU_NOUN_ENDINGS
    }
    offenders = set()
    for text in read_archive_texts():
        for hit in kf.find(text):
            if hit.matched_text not in allowed:
                offenders.add((hit.keyword, hit.matched_text))
    assert not offenders, f"filter matched non-inflections: {sorted(offenders)}"


@requires_archive
def test_fixture_still_agrees_with_the_archive():
    """Catches a stale fixture after the archive grows."""
    kf = build_filter()
    texts = read_archive_texts()
    matching = sum(1 for t in texts if kf.matches(t))
    corpus = FIXTURE["corpus"]
    assert (len(texts), matching) == (
        corpus["posts_with_text"],
        corpus["posts_matching"],
    ), "archive changed -- rerun scripts/make_fixtures.py"


@requires_archive
def test_fixture_pins_every_form_the_archive_produces():
    """The real anti-regression guard, when the archive is available.

    Offline the suite can only check the forms someone chose to freeze; here
    it checks that nothing in the archive goes unasserted.
    """
    kf = build_filter()
    in_archive = {
        hit.matched_text for text in read_archive_texts() for hit in kf.find(text)
    }
    missing = in_archive - covered_forms()
    assert not missing, f"unpinned forms -- rerun make_fixtures.py: {sorted(missing)}"
