"""SQLite storage. The only writer in the system.

Tables map 1:1 onto the locked domain model: Source, Research Item, Media
Asset, Structured Field, Note. Structured Field and Note stay separate tables
so the fact/opinion split is enforced by the schema rather than by convention.

Matched keywords are written as Structured Fields with origin='system' --
they are mechanical facts about the text, so they belong in the same place as
every other extracted fact, and they carry provenance from day one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..domain import RawPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    handle TEXT,
    title TEXT,
    first_tracked_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, platform_id)
);

CREATE TABLE IF NOT EXISTS research_item (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source(id),
    external_id TEXT NOT NULL,
    original_url TEXT,
    posted_at TEXT,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

-- Exact-duplicate detection (plan tier 1). Not UNIQUE: a genuine repost is
-- still its own observation, so duplicates are detected, never rejected by
-- the database.
CREATE INDEX IF NOT EXISTS idx_item_hash ON research_item(content_hash);

CREATE TABLE IF NOT EXISTS media_asset (
    id INTEGER PRIMARY KEY,
    research_item_id INTEGER NOT NULL REFERENCES research_item(id),
    kind TEXT NOT NULL,
    storage_path TEXT,
    original_url TEXT,
    file_name TEXT,
    size_bytes INTEGER,
    duration INTEGER
);

CREATE TABLE IF NOT EXISTS structured_field (
    id INTEGER PRIMARY KEY,
    research_item_id INTEGER NOT NULL REFERENCES research_item(id),
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('system', 'human')),
    model TEXT,
    prompt_version TEXT,
    confidence REAL
);

-- Human analysis only. Nothing automated ever writes here.
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY,
    research_item_id INTEGER NOT NULL REFERENCES research_item(id),
    body TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'human' CHECK (author = 'human'),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Full-text search, kept separate because it is an FTS5 virtual table plus a
# trigger rather than a plain table. Stage 8 calls for full-text search in the
# MVP; the trigger keeps the index in step with inserts automatically.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS research_item_fts
USING fts5(raw_text, content='research_item', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS research_item_ai AFTER INSERT ON research_item BEGIN
    INSERT INTO research_item_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
"""


class SqliteStore:
    """Writes collected posts. Nothing else in the pipeline touches the DB."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.executescript(FTS_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _source_id(self, post: RawPost) -> int:
        cur = self._conn.execute(
            "INSERT INTO source (platform, platform_id, handle, title) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (platform, platform_id) DO UPDATE "
            "SET title = COALESCE(excluded.title, source.title) RETURNING id",
            (
                post.source.platform,
                post.source.platform_id,
                post.source.handle,
                post.source.title,
            ),
        )
        return int(cur.fetchone()[0])

    def save(self, post: RawPost, keywords: tuple[str, ...]) -> bool:
        """Store a collected post. False if this source already had it.

        Re-running an ingest must not duplicate items, so (source, external_id)
        is the idempotency key.
        """
        source_id = self._source_id(post)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO research_item "
            "(source_id, external_id, original_url, posted_at, raw_text, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_id,
                post.external_id,
                post.original_url,
                post.posted_at.isoformat() if post.posted_at else None,
                post.text or "",
                post.content_hash(),
            ),
        )
        if cur.rowcount == 0:
            self._conn.commit()
            return False

        item_id = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT INTO structured_field (research_item_id, name, value, origin) "
            "VALUES (?, 'keyword', ?, 'system')",
            [(item_id, keyword) for keyword in keywords],
        )
        self._conn.executemany(
            "INSERT INTO media_asset (research_item_id, kind, storage_path, "
            "original_url, file_name, size_bytes, duration) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    item_id,
                    m.kind,
                    m.path,
                    m.original_url,
                    m.file_name,
                    m.size_bytes,
                    m.duration,
                )
                for m in post.media
            ],
        )
        self._conn.commit()
        return True

    # -- human input, from the review interface -----------------------------

    def add_note(self, item_id: int, body: str) -> int:
        """Record a human note. Nothing automated may call this.

        The note table's CHECK constrains author to 'human', so an automated
        writer cannot quietly file its output as analysis.
        """
        body = body.strip()
        if not body:
            raise ValueError("a note needs a body")
        self._require_item(item_id)
        cur = self._conn.execute(
            "INSERT INTO note (research_item_id, body) VALUES (?, ?)", (item_id, body)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def add_field(self, item_id: int, name: str, value: str) -> int:
        """Record a manually-observed Structured Field, marked origin='human'.

        Provenance is the whole point: a field added here is distinguishable
        forever from one a model produced.
        """
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError("a field needs both a name and a value")
        self._require_item(item_id)
        cur = self._conn.execute(
            "INSERT INTO structured_field (research_item_id, name, value, origin) "
            "VALUES (?, ?, ?, 'human')",
            (item_id, name, value),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def _require_item(self, item_id: int) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM research_item WHERE id = ?", (item_id,)
        ).fetchone()
        if exists is None:
            raise KeyError(f"no research item with id {item_id}")

    # -- read-only helpers, for the CLI and for tests ----------------------

    def count_items(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM research_item").fetchone()[0]
        )

    def all_texts(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute("SELECT raw_text FROM research_item")
        ]

    def keywords_for(self, external_id: str) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT f.value FROM structured_field f "
                "JOIN research_item i ON i.id = f.research_item_id "
                "WHERE i.external_id = ? AND f.name = 'keyword' ORDER BY f.id",
                (external_id,),
            )
        ]

    def duplicate_hashes(self) -> list[tuple[str, int]]:
        return [
            (row[0], int(row[1]))
            for row in self._conn.execute(
                "SELECT content_hash, COUNT(*) c FROM research_item "
                "GROUP BY content_hash HAVING c > 1 ORDER BY c DESC"
            )
        ]
