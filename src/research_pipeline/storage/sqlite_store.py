"""SQLite storage. The only writer in the system.

Tables map 1:1 onto the locked domain model: Source, Research Item, Media
Asset, Structured Field, Note. Structured Field and Note stay separate tables
so the fact/opinion split is enforced by the schema rather than by convention.

Matched keywords are written as Structured Fields with origin='system' --
they are mechanical facts about the text, so they belong in the same place as
every other extracted fact, and they carry provenance from day one.

Machine output (plan §3) is additive here by construction: cleaning writes to
`cleaned_text`, extraction inserts new `structured_field` rows, and a trigger
makes `raw_text` refuse to change at all. The human can always read what the
model read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..domain import RawPost
from ..filtering.keywords import KEYWORD_PRODUCER, KEYWORD_VERSION
from ..llm.protocol import Provenance

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
    -- Machine-derived, all nullable: an item is complete without them, and
    -- stays complete if the model stages are switched off.
    cleaned_text TEXT,
    cleaned_by_model TEXT,
    cleaned_prompt_version TEXT,
    simhash TEXT,
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

-- Plan §3, guardrail 2: machine output is traceable.
--
-- The columns above are nullable because a field the *human* observed needs no
-- model. A field a machine produced and cannot name its producer for is a
-- different thing: it cannot be re-run and it cannot be disproved, which is
-- the whole basis on which §3 permits machine output at all. A trigger rather
-- than a CHECK so an existing database gains the rule too -- SQLite cannot
-- add a constraint to a table that already exists, but it can add a trigger.
CREATE TRIGGER IF NOT EXISTS structured_field_system_is_traceable
BEFORE INSERT ON structured_field
FOR EACH ROW WHEN NEW.origin = 'system'
    AND (NEW.model IS NULL OR NEW.prompt_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'a system field needs model and prompt_version');
END;

-- Plan §3, guardrail 1: raw is immutable.
--
-- Cleaning and extraction are only safe because the human can always compare
-- their output against the text the model was given. A trigger enforces that
-- the same way the note table's CHECK enforces the fact/opinion split: in the
-- schema, where no future caller can forget it. Note ABORT rolls back the
-- statement, so a well-meaning UPDATE cannot half-apply.
CREATE TRIGGER IF NOT EXISTS research_item_raw_text_is_immutable
BEFORE UPDATE OF raw_text ON research_item
FOR EACH ROW WHEN NEW.raw_text IS NOT OLD.raw_text
BEGIN
    SELECT RAISE(ABORT, 'raw_text is immutable: write cleaned_text instead');
END;

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
        self._add_missing_columns()
        self._conn.commit()

    def _add_missing_columns(self) -> None:
        """Bring an existing store up to the current schema.

        `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
        exists, so a database written before the machine columns arrived would
        otherwise keep an older shape and fail on the first cleaned write. The
        columns are nullable and unindexed, so adding them is instant and does
        not touch a single collected row.
        """
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(research_item)")
        }
        for column, kind in (
            ("cleaned_text", "TEXT"),
            ("cleaned_by_model", "TEXT"),
            ("cleaned_prompt_version", "TEXT"),
            ("simhash", "TEXT"),
        ):
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE research_item ADD COLUMN {column} {kind}"
                )

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
            "INSERT INTO structured_field (research_item_id, name, value, origin, "
            "model, prompt_version) VALUES (?, 'keyword', ?, 'system', ?, ?)",
            [
                (item_id, keyword, KEYWORD_PRODUCER, KEYWORD_VERSION)
                for keyword in keywords
            ],
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

    # -- machine output, from the model stages ------------------------------

    def set_cleaned_text(
        self, item_id: int, cleaned_text: str, provenance: Provenance
    ) -> None:
        """Store a cleaned copy of the text. The raw text is not touched.

        Idempotent by `prompt_version`: re-running the same cleaner over the
        same item overwrites its own previous output rather than accumulating
        copies, which is what makes plan §5's re-run safety real.
        """
        self._require_item(item_id)
        self._conn.execute(
            "UPDATE research_item SET cleaned_text = ?, cleaned_by_model = ?, "
            "cleaned_prompt_version = ? WHERE id = ?",
            (cleaned_text, provenance.model, provenance.prompt_version, item_id),
        )
        self._conn.commit()

    def add_machine_field(
        self, item_id: int, name: str, value: str, provenance: Provenance
    ) -> int:
        """Record an extracted fact, marked origin='system' and traceable.

        The mirror of `add_field`: same table, same shape, opposite origin. A
        field written here can always be told apart from one the human observed
        -- and, unlike the human's, it says which model and which prompt
        produced it, so it can be disproved and re-run.
        """
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError("a field needs both a name and a value")
        self._require_item(item_id)

        # Idempotent on the whole claim, not just the value: the same fact,
        # from the same producer, under the same prompt version, is the same
        # row. Without this a re-run silently doubles every extracted field --
        # and plan §5 requires re-runs to be safe, which has to mean safe in
        # the store and not merely safe against the API.
        #
        # A *different* model or version producing the same value is a
        # separate row on purpose: two independent producers agreeing is
        # evidence, and collapsing them would destroy it.
        existing = self._conn.execute(
            "SELECT id FROM structured_field WHERE research_item_id = ? AND name = ? "
            "AND value = ? AND origin = 'system' AND model IS ? AND prompt_version IS ?",
            (item_id, name, value, provenance.model, provenance.prompt_version),
        ).fetchone()
        if existing is not None:
            return int(existing[0])

        cur = self._conn.execute(
            "INSERT INTO structured_field "
            "(research_item_id, name, value, origin, model, prompt_version, confidence) "
            "VALUES (?, ?, ?, 'system', ?, ?, ?)",
            (
                item_id,
                name,
                value,
                provenance.model,
                provenance.prompt_version,
                provenance.confidence,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def set_simhash(self, item_id: int, value: str) -> None:
        """Record the near-duplicate fingerprint (plan §7 tier 2). No model."""
        self._require_item(item_id)
        self._conn.execute(
            "UPDATE research_item SET simhash = ? WHERE id = ?", (value, item_id)
        )
        self._conn.commit()

    def backfill_keyword_provenance(self) -> int:
        """Attribute pre-existing keyword fields to the gate that produced them.

        Keyword fields written before provenance was required carry origin
        'system' and nothing else. The trigger only guards new inserts, so
        those rows sit outside the guarantee -- and they are the one case where
        the missing provenance is knowable rather than lost: they were produced
        by the gate, under the matching rules the version names.

        Deliberately not run automatically. It edits stored rows, and a
        migration that rewrites collected data without being asked is exactly
        the kind of thing this system is built not to do.
        """
        cur = self._conn.execute(
            "UPDATE structured_field SET model = ?, prompt_version = ? "
            "WHERE name = 'keyword' AND origin = 'system' AND model IS NULL",
            (KEYWORD_PRODUCER, KEYWORD_VERSION),
        )
        self._conn.commit()
        return cur.rowcount

    def untraceable_system_fields(self) -> int:
        """How many machine fields predate the provenance requirement."""
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM structured_field WHERE origin = 'system' "
                "AND (model IS NULL OR prompt_version IS NULL)"
            ).fetchone()[0]
        )

    def items_needing(self, prompt_version: str, *, limit: int | None = None):
        """Items this prompt version has not cleaned yet, oldest first.

        The idempotency key from plan §5 as a query: re-running a stage picks
        up where it stopped instead of paying for every post again.
        """
        sql = (
            "SELECT id, raw_text FROM research_item "
            "WHERE cleaned_prompt_version IS NOT ? ORDER BY id"
        )
        params: tuple = (prompt_version,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (prompt_version, limit)
        return [(int(row[0]), row[1]) for row in self._conn.execute(sql, params)]

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
