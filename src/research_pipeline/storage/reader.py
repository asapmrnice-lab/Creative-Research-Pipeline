"""Read-only view of the store, for the Review interface and Search.

Opened with SQLite's `mode=ro`, so a bug here cannot corrupt collected data --
the read side is prevented from writing by the connection itself, not by
politeness. Writes go through SqliteStore, which stays the only writer.

The queries return plain dataclasses rather than sqlite3.Row, so the CLI never
has to know the column order or the schema.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# FTS5 treats these as operators, so a query containing one is passed through
# untouched instead of being turned into a prefix search.
_FTS_OPERATORS = ('"', "*", ":", "(", ")", " OR ", " AND ", " NOT ", "NEAR")


@dataclass(frozen=True)
class ItemSummary:
    """One row of a list view."""

    id: int
    posted_at: str | None
    channel: str
    keywords: tuple[str, ...]
    note_count: int
    preview: str
    url: str | None = None

    @property
    def reviewed(self) -> bool:
        """Workflow 2 defines un-reviewed as "has no Notes yet"."""
        return self.note_count > 0


@dataclass(frozen=True)
class Note:
    body: str
    author: str
    created_at: str


@dataclass(frozen=True)
class Field:
    name: str
    value: str
    origin: str


@dataclass(frozen=True)
class Media:
    kind: str
    file_name: str | None
    storage_path: str | None


@dataclass(frozen=True)
class ItemDetail:
    """Everything held about one item, assembled for a single view."""

    id: int
    external_id: str
    posted_at: str | None
    ingested_at: str
    channel: str
    handle: str | None
    url: str | None
    text: str
    fields: tuple[Field, ...] = ()
    notes: tuple[Note, ...] = ()
    media: tuple[Media, ...] = ()

    @property
    def keywords(self) -> tuple[str, ...]:
        return tuple(f.value for f in self.fields if f.name == "keyword")


@dataclass(frozen=True)
class SearchHit:
    id: int
    posted_at: str | None
    channel: str
    snippet: str


@dataclass
class StoreStats:
    items: int = 0
    sources: int = 0
    notes: int = 0
    media: int = 0
    unreviewed: int = 0
    keywords: dict[str, int] = field(default_factory=dict)
    first_post: str | None = None
    last_post: str | None = None


def _preview(text: str, width: int) -> str:
    """First meaningful line, collapsed to one line and clipped."""
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if stripped:
            return stripped[:width] + ("…" if len(stripped) > width else "")
    return ""


class ResearchStoreReader:
    """Read-only access to the research store."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"no store at {self.db_path} -- run scripts/ingest.py first"
            )
        self._conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ResearchStoreReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- list --------------------------------------------------------------

    def list_items(
        self,
        *,
        limit: int | None = None,
        unreviewed_only: bool = False,
        preview_width: int = 60,
    ) -> list[ItemSummary]:
        """Newest first -- a review session starts with what just arrived."""
        sql = """
            SELECT i.id, i.posted_at, COALESCE(s.title, s.platform_id), i.raw_text,
                   i.original_url,
                   (SELECT COUNT(*) FROM note n WHERE n.research_item_id = i.id),
                   (SELECT GROUP_CONCAT(f.value, '|') FROM structured_field f
                     WHERE f.research_item_id = i.id AND f.name = 'keyword')
            FROM research_item i
            JOIN source s ON s.id = i.source_id
        """
        if unreviewed_only:
            sql += " WHERE NOT EXISTS (SELECT 1 FROM note n WHERE n.research_item_id = i.id)"
        sql += " ORDER BY i.posted_at DESC, i.id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)

        return [
            ItemSummary(
                id=int(row[0]),
                posted_at=row[1],
                channel=row[2],
                keywords=tuple(row[6].split("|")) if row[6] else (),
                note_count=int(row[5]),
                preview=_preview(row[3] or "", preview_width),
                url=row[4],
            )
            for row in self._conn.execute(sql, params)
        ]

    # -- view --------------------------------------------------------------

    def get_item(self, item_id: int) -> ItemDetail | None:
        row = self._conn.execute(
            "SELECT i.id, i.external_id, i.posted_at, i.ingested_at, "
            "COALESCE(s.title, s.platform_id), s.handle, i.original_url, i.raw_text "
            "FROM research_item i JOIN source s ON s.id = i.source_id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return None

        fields = tuple(
            Field(name=r[0], value=r[1], origin=r[2])
            for r in self._conn.execute(
                "SELECT name, value, origin FROM structured_field "
                "WHERE research_item_id = ? ORDER BY origin, id",
                (item_id,),
            )
        )
        notes = tuple(
            Note(body=r[0], author=r[1], created_at=r[2])
            for r in self._conn.execute(
                "SELECT body, author, created_at FROM note "
                "WHERE research_item_id = ? ORDER BY id",
                (item_id,),
            )
        )
        media = tuple(
            Media(kind=r[0], file_name=r[1], storage_path=r[2])
            for r in self._conn.execute(
                "SELECT kind, file_name, storage_path FROM media_asset "
                "WHERE research_item_id = ? ORDER BY id",
                (item_id,),
            )
        )
        return ItemDetail(
            id=int(row[0]),
            external_id=row[1],
            posted_at=row[2],
            ingested_at=row[3],
            channel=row[4],
            handle=row[5],
            url=row[6],
            text=row[7],
            fields=fields,
            notes=notes,
            media=media,
        )

    # -- search ------------------------------------------------------------

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        """Full-text search over collected text (MVP tier: FTS5, no embeddings).

        A bare word becomes a prefix query, because Russian inflects: searching
        "крео" should find "креативы" the same way the collection filter does.
        Anything already carrying FTS syntax is passed through as written.
        """
        match = query.strip()
        if not match:
            return []
        if not any(op in match.upper() for op in _FTS_OPERATORS):
            match = " ".join(f"{word}*" for word in match.split())

        rows = self._conn.execute(
            "SELECT i.id, i.posted_at, COALESCE(s.title, s.platform_id), "
            "snippet(research_item_fts, 0, '>>', '<<', '…', 12) "
            "FROM research_item_fts f "
            "JOIN research_item i ON i.id = f.rowid "
            "JOIN source s ON s.id = i.source_id "
            "WHERE research_item_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
        return [
            SearchHit(
                id=int(r[0]),
                posted_at=r[1],
                channel=r[2],
                snippet=" ".join((r[3] or "").split()),
            )
            for r in rows
        ]

    # -- stats -------------------------------------------------------------

    def stats(self) -> StoreStats:
        one = lambda sql: int(self._conn.execute(sql).fetchone()[0])  # noqa: E731
        first, last = self._conn.execute(
            "SELECT MIN(posted_at), MAX(posted_at) FROM research_item"
        ).fetchone()
        return StoreStats(
            items=one("SELECT COUNT(*) FROM research_item"),
            sources=one("SELECT COUNT(*) FROM source"),
            notes=one("SELECT COUNT(*) FROM note"),
            media=one("SELECT COUNT(*) FROM media_asset"),
            unreviewed=one(
                "SELECT COUNT(*) FROM research_item i WHERE NOT EXISTS "
                "(SELECT 1 FROM note n WHERE n.research_item_id = i.id)"
            ),
            keywords={
                r[0]: int(r[1])
                for r in self._conn.execute(
                    "SELECT value, COUNT(*) c FROM structured_field "
                    "WHERE name = 'keyword' GROUP BY value ORDER BY c DESC, value"
                )
            },
            first_post=first,
            last_post=last,
        )

    # -- export ------------------------------------------------------------

    def export_rows(self) -> list[dict[str, str]]:
        """Flat table snapshot -- a disposable view, never a second source of truth."""
        return [
            {
                "id": str(item.id),
                "posted_at": item.posted_at or "",
                "channel": item.channel,
                "matched_keywords": ", ".join(item.keywords),
                "notes": str(item.note_count),
                "url": item.url or "",
                "text": self._text_of(item.id),
            }
            for item in self.list_items(preview_width=10**6)
        ]

    def _text_of(self, item_id: int) -> str:
        row = self._conn.execute(
            "SELECT raw_text FROM research_item WHERE id = ?", (item_id,)
        ).fetchone()
        return row[0] if row else ""
