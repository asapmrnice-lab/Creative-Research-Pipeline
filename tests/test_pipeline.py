"""Prove the filter is connected, not merely present.

The failure this suite exists to catch is the one the trial tool had: posts
get stored regardless of what the filter thinks. So these tests do not ask
"does the filter work" -- test_keywords.py does that. They ask "can a post
without a keyword reach storage", and the answer has to be no, by
construction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from research_pipeline.domain import RawMedia, RawPost, RawSource
from research_pipeline.filtering import (
    KeywordFilter,
    KeywordFilterConfig,
    KeywordGate,
    Verdict,
)
from research_pipeline.pipeline import ingest
from research_pipeline.storage.sqlite_store import SqliteStore

KEYWORDS = ("креатив", "креативчик", "крео", "креос", "кейс")
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "real_posts.json").read_text(encoding="utf-8")
)


@pytest.fixture
def gate() -> KeywordGate:
    return KeywordGate(KeywordFilter(KeywordFilterConfig(keywords=KEYWORDS)))


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    with SqliteStore(tmp_path / "research.db") as s:
        yield s


def post(text: str | None, external_id: str = "chat:1", **kw) -> RawPost:
    return RawPost(
        source=RawSource(platform="telegram", platform_id="chat", title="Test"),
        external_id=external_id,
        text=text,
        posted_at=datetime(2026, 1, 1, 12, 0),
        **kw,
    )


class ListSource:
    def __init__(self, posts): self._posts = posts
    def fetch(self): return iter(self._posts)


# --------------------------------------------------------------------------
# The gate's verdicts
# --------------------------------------------------------------------------


def test_gate_collects_a_post_with_a_keyword(gate):
    decision = gate.evaluate(post("новые креативы приехали"))
    assert decision.collect
    assert decision.keywords == ("креатив",)


def test_gate_rejects_a_post_without_a_keyword(gate):
    decision = gate.evaluate(post("как увеличить лимит на БМе в Facebook"))
    assert not decision.collect
    assert decision.verdict is Verdict.NO_KEYWORD
    assert decision.keywords == ()


@pytest.mark.parametrize("text", [None, "", "   ", "\n\n"])
def test_gate_separates_media_only_posts_from_rejected_ones(gate, text):
    """A photo with no caption was never read, so it is not a rejection."""
    assert gate.evaluate(post(text)).verdict is Verdict.NO_TEXT


def test_gate_rejects_compounds(gate):
    assert not gate.evaluate(post("антикейс на 1 500 000")).collect
    assert not gate.evaluate(post("видео-креативов много")).collect


# --------------------------------------------------------------------------
# The connection: what reaches storage
# --------------------------------------------------------------------------


def test_keywordless_post_never_reaches_storage(gate, store):
    result = ingest(ListSource([post("обычный пост без ключевых слов")]), gate, store)

    assert result.collected == 0
    assert store.count_items() == 0
    assert result.rejected == {"no-keyword": 1}


def test_only_matching_posts_are_stored(gate, store):
    posts = [
        post("нужен креатив на завтра", "chat:1"),
        post("как увеличить лимит на БМе", "chat:2"),
        post("разбор кейсов за июль", "chat:3"),
        post(None, "chat:4"),
        post("антикейс на 1 500 000", "chat:5"),
    ]
    result = ingest(ListSource(posts), gate, store)

    assert store.count_items() == 2
    assert store.all_texts() == ["нужен креатив на завтра", "разбор кейсов за июль"]
    assert result.rejected == {"no-keyword": 2, "no-text": 1}


def test_media_of_a_rejected_post_is_never_stored(gate, store):
    """The cost the trial tool paid: 297 MB of media for unwanted posts."""
    heavy = post(
        "обычный пост без ключевых слов",
        media=(RawMedia(kind="video", path="/x.mp4", size_bytes=50_000_000),),
    )
    ingest(ListSource([heavy]), gate, store)

    media_count = store._conn.execute("SELECT COUNT(*) FROM media_asset").fetchone()[0]
    assert media_count == 0


def test_matched_keywords_are_stored_as_system_facts(gate, store):
    ingest(ListSource([post("креатив и кейс в одном посте", "chat:9")]), gate, store)

    assert store.keywords_for("chat:9") == ["креатив", "кейс"]
    origins = {
        row[0]
        for row in store._conn.execute("SELECT origin FROM structured_field")
    }
    assert origins == {"system"}


def test_notes_table_refuses_non_human_authors(store):
    """The fact/opinion split, enforced by the schema rather than by habit."""
    store._conn.execute(
        "INSERT INTO source (platform, platform_id) VALUES ('telegram', 'c')"
    )
    store._conn.execute(
        "INSERT INTO research_item (source_id, external_id, raw_text, content_hash) "
        "VALUES (1, 'x', 't', 'h')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO note (research_item_id, body, author) VALUES (1, 'b', 'system')"
        )


# --------------------------------------------------------------------------
# Re-running must be safe
# --------------------------------------------------------------------------


def test_running_twice_stores_each_post_once(gate, store):
    posts = [post("нужен креатив на завтра", "chat:1")]

    first = ingest(ListSource(posts), gate, store)
    second = ingest(ListSource(posts), gate, store)

    assert (first.stored, second.stored) == (1, 0)
    assert second.duplicates == 1
    assert store.count_items() == 1


def test_counts_always_add_up(gate, store):
    posts = [
        post("креатив", "a"),
        post("ничего", "b"),
        post(None, "c"),
        post("кейс", "d"),
    ]
    result = ingest(ListSource(posts), gate, store)

    assert result.seen == result.collected + sum(result.rejected.values())
    assert result.seen == 4


# --------------------------------------------------------------------------
# Against real posts, end to end
# --------------------------------------------------------------------------


def test_real_keywordless_posts_are_all_refused_by_the_pipeline(gate, store):
    """Every real negative in the fixture, run through the actual pipeline."""
    posts = [
        post(p["text"], f"real:{p['msg_id']}") for p in FIXTURE["non_matching"]
    ]
    result = ingest(ListSource(posts), gate, store)

    assert store.count_items() == 0
    assert result.rejected["no-keyword"] == len(posts)


def test_real_matching_posts_are_all_collected_by_the_pipeline(gate, store):
    # The fixture can hold two excerpts of one post (different word forms), so
    # ids are made unique here -- otherwise the store would correctly treat the
    # second as a duplicate and this would be testing dedup, not collection.
    posts = [
        post(p["text"], f"real:{i}:{p['msg_id']}")
        for i, p in enumerate(FIXTURE["matching"])
    ]
    result = ingest(ListSource(posts), gate, store)

    assert result.collected == len(posts)
    assert store.count_items() == len(posts)
