"""Prove the review interface reads the store and respects its boundaries.

Two failures are worth catching here. First, that the read side could write:
the reader opens a read-only connection, so an accidental UPDATE has to raise
rather than silently mutate collected data. Second, that provenance could be
lost: a human note or field must be recorded as human, since the whole
fact/opinion split rests on that column.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from research_pipeline.domain import RawPost, RawSource
from research_pipeline.storage.reader import ResearchStoreReader
from research_pipeline.storage.sqlite_store import SqliteStore

POSTS = [
    ("креативы решают всё", ("креатив",), datetime(2026, 5, 10, 9, 0)),
    ("разбираем кейс на 300к", ("кейс",), datetime(2026, 6, 1, 12, 0)),
    ("новые крео в работе", ("крео",), datetime(2026, 7, 20, 8, 0)),
]


def post(text: str, n: int, when: datetime) -> RawPost:
    return RawPost(
        source=RawSource(
            platform="telegram", platform_id="chat", handle="ch", title="Test Channel"
        ),
        external_id=f"chat:{n}",
        text=text,
        posted_at=when,
        original_url=f"https://t.me/ch/{n}",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "research.db"
    with SqliteStore(path) as store:
        for n, (text, keywords, when) in enumerate(POSTS, start=1):
            store.save(post(text, n, when), keywords)
    return path


@pytest.fixture
def reader(db_path: Path) -> ResearchStoreReader:
    with ResearchStoreReader(db_path) as r:
        yield r


def test_missing_store_says_what_to_run(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="ingest.py"):
        ResearchStoreReader(tmp_path / "nope.db")


def test_reader_cannot_write(reader: ResearchStoreReader):
    """The read side is read-only by connection, not by convention."""
    with pytest.raises(sqlite3.OperationalError):
        reader._conn.execute("DELETE FROM research_item")


def test_list_is_newest_first_and_carries_keywords(reader: ResearchStoreReader):
    items = reader.list_items()
    assert [i.preview for i in items] == [
        "новые крео в работе",
        "разбираем кейс на 300к",
        "креативы решают всё",
    ]
    assert items[0].keywords == ("крео",)
    assert items[0].channel == "Test Channel"


def test_list_limit(reader: ResearchStoreReader):
    assert len(reader.list_items(limit=2)) == 2


def test_view_returns_none_for_unknown_id(reader: ResearchStoreReader):
    assert reader.get_item(999) is None


def test_view_carries_link_and_matched_keyword(reader: ResearchStoreReader):
    item = reader.get_item(reader.list_items()[0].id)
    assert item.keywords == ("крео",)
    assert item.url == "https://t.me/ch/3"
    assert item.text == "новые крео в работе"


def test_search_bare_word_matches_inflections(reader: ResearchStoreReader):
    """"креатив" must find "креативы", the way the collection filter does."""
    hits = reader.search("креатив")
    assert [h.id for h in hits] == [1]
    assert ">>креативы<<" in hits[0].snippet


def test_search_empty_query_is_not_a_wildcard(reader: ResearchStoreReader):
    assert reader.search("   ") == []


def test_search_miss_returns_nothing(reader: ResearchStoreReader):
    assert reader.search("вебинар") == []


def test_note_marks_item_reviewed(db_path: Path):
    with SqliteStore(db_path) as store:
        store.add_note(1, "worth reusing")

    with ResearchStoreReader(db_path) as reader:
        assert [i.id for i in reader.list_items(unreviewed_only=True)] == [3, 2]
        item = reader.get_item(1)
        assert item.notes[0].body == "worth reusing"
        assert item.notes[0].author == "human"
        assert reader.stats().unreviewed == 2


def test_manual_field_is_recorded_as_human(db_path: Path):
    with SqliteStore(db_path) as store:
        store.add_field(1, "geo", "UA")

    with ResearchStoreReader(db_path) as reader:
        item = reader.get_item(1)
        origins = {f.name: f.origin for f in item.fields}
        assert origins == {"keyword": "system", "geo": "human"}
        # A manual field must not be mistaken for a matched keyword.
        assert item.keywords == ("креатив",)


@pytest.mark.parametrize("body", ["", "   "])
def test_empty_note_is_refused(db_path: Path, body: str):
    with SqliteStore(db_path) as store:
        with pytest.raises(ValueError):
            store.add_note(1, body)


def test_writing_to_a_missing_item_is_refused(db_path: Path):
    with SqliteStore(db_path) as store:
        with pytest.raises(KeyError):
            store.add_note(999, "ghost")
        with pytest.raises(KeyError):
            store.add_field(999, "geo", "UA")


def test_stats_tally_matches_the_store(reader: ResearchStoreReader):
    s = reader.stats()
    assert s.items == 3
    assert s.sources == 1
    assert s.unreviewed == 3
    assert s.keywords == {"кейс": 1, "крео": 1, "креатив": 1}
    assert s.first_post.startswith("2026-05-10")
    assert s.last_post.startswith("2026-07-20")


def test_export_rows_carry_full_text_not_a_preview(reader: ResearchStoreReader):
    rows = reader.export_rows()
    assert len(rows) == 3
    assert rows[0]["text"] == "новые крео в работе"
    assert rows[0]["matched_keywords"] == "крео"
