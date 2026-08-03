"""Supabase (Postgres) storage -- the writer and the read-only view.

Why a direct Postgres connection rather than the PostgREST client
-----------------------------------------------------------------
Supabase supports both. This module uses `psycopg` against the project's
Postgres connection string, because the read side is the demanding half:
`stats()` is five aggregates and a GROUP BY, and `search()` needs
`ts_headline` to build snippets. Through PostgREST each of those has to become
a stored RPC function, which moves half the read logic out of this file and
into SQL that no test can reach. Over a direct connection they stay one
statement each, close to the SQLite versions they must agree with.

It also buys the guarantee the SQLite reader already has. `ResearchStoreReader`
opens its connection `mode=ro` so a stray write raises rather than corrupting
collected data; here the reader sets `default_transaction_read_only`, which is
the same promise enforced by the same kind of mechanism.

`psycopg` is imported lazily by the composition root, so the default SQLite
backend never requires it to be installed.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..domain import RawPost
from .views import (
    Field,
    ItemDetail,
    ItemSummary,
    Media,
    Note,
    SearchHit,
    StoreStats,
    preview,
)

# Characters that mean something to to_tsquery. A post title is not a query
# language, so they are stripped from bare words rather than escaped.
_TSQUERY_SPECIAL = re.compile(r"[&|!():*<>'\"\\]")

# If the human typed any of these they meant them, and websearch_to_tsquery
# understands them directly.
_WEBSEARCH_HINTS = ('"', " OR ", " -")


def _iso(value) -> str | None:
    """Postgres hands back datetimes; the view types are backend-neutral strings.

    Both backends must render a date identically or the CLI output would change
    depending on which store answered.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _connect(dsn: str, *, read_only: bool):
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
        raise RuntimeError(
            "the supabase backend needs psycopg -- install it with\n"
            "    pip install \"psycopg[binary]\"\n"
            "or set STORE_BACKEND=sqlite to work locally."
        ) from exc

    options = "-c default_transaction_read_only=on" if read_only else None
    return psycopg.connect(dsn, options=options, autocommit=False)


class SupabaseStore:
    """Writes collected posts and the human's annotations.

    The Postgres counterpart of SqliteStore, and deliberately the same shape:
    the pipeline cannot tell them apart, which is the whole point of the
    `Store` protocol.
    """

    def __init__(self, dsn: str, service_key: str | None = None) -> None:
        # service_key is accepted so the composition root can pass what it has
        # without knowing which transport this backend chose. Unused here: a
        # direct Postgres connection authenticates with the DSN.
        self._conn = _connect(dsn, read_only=False)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SupabaseStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _source_id(self, post: RawPost) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source (platform, platform_id, handle, title) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (platform, platform_id) "
                "DO UPDATE SET title = COALESCE(EXCLUDED.title, source.title) "
                "RETURNING id",
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

        One transaction: an item never lands without the keywords that let it
        in, so a crash mid-save cannot leave an item whose provenance is gone.
        """
        with self._conn.transaction():
            source_id = self._source_id(post)
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research_item (source_id, external_id, original_url, "
                    "posted_at, raw_text, content_hash) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (source_id, external_id) DO NOTHING RETURNING id",
                    (
                        source_id,
                        post.external_id,
                        post.original_url,
                        post.posted_at,
                        post.text or "",
                        post.content_hash(),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                item_id = int(row[0])

                cur.executemany(
                    "INSERT INTO structured_field (research_item_id, name, value, origin) "
                    "VALUES (%s, 'keyword', %s, 'system')",
                    [(item_id, keyword) for keyword in keywords],
                )
                cur.executemany(
                    "INSERT INTO media_asset (research_item_id, kind, storage_path, "
                    "original_url, file_name, size_bytes, duration) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
        return True

    # -- human input, from the review interface -----------------------------

    def add_note(self, item_id: int, body: str) -> int:
        """Record a human note. Nothing automated may call this."""
        body = body.strip()
        if not body:
            raise ValueError("a note needs a body")
        self._require_item(item_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO note (research_item_id, body) VALUES (%s, %s) RETURNING id",
                (item_id, body),
            )
            new_id = int(cur.fetchone()[0])
        self._conn.commit()
        return new_id

    def add_field(self, item_id: int, name: str, value: str) -> int:
        """Record a manually-observed Structured Field, marked origin='human'."""
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError("a field needs both a name and a value")
        self._require_item(item_id)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO structured_field (research_item_id, name, value, origin) "
                "VALUES (%s, %s, %s, 'human') RETURNING id",
                (item_id, name, value),
            )
            new_id = int(cur.fetchone()[0])
        self._conn.commit()
        return new_id

    def _require_item(self, item_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM research_item WHERE id = %s", (item_id,))
            if cur.fetchone() is None:
                raise KeyError(f"no research item with id {item_id}")

    # -- read-only helpers, matching SqliteStore ---------------------------

    def count_items(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM research_item")
            return int(cur.fetchone()[0])


def _tsquery(query: str) -> tuple[str, str]:
    """Turn what the human typed into (sql_function, argument).

    Mirrors the SQLite reader's rule: a bare word becomes a prefix match, so
    searching "крео" finds "креативы" the same way the collection filter does.
    Anything carrying search syntax is handed to websearch_to_tsquery, which
    understands quoted phrases, OR and leading minus.
    """
    if any(hint in f" {query.upper()} " for hint in _WEBSEARCH_HINTS):
        return "websearch_to_tsquery", query
    words = [_TSQUERY_SPECIAL.sub("", w) for w in query.split()]
    return "to_tsquery", " & ".join(f"{w}:*" for w in words if w)


class SupabaseReadStore:
    """Read-only access to the research store. Cannot write, by connection."""

    def __init__(self, dsn: str, service_key: str | None = None) -> None:
        self._conn = _connect(dsn, read_only=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SupabaseReadStore":
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
                   (SELECT array_agg(f.value ORDER BY f.id) FROM structured_field f
                     WHERE f.research_item_id = i.id AND f.name = 'keyword')
            FROM research_item i
            JOIN source s ON s.id = i.source_id
        """
        if unreviewed_only:
            sql += " WHERE NOT EXISTS (SELECT 1 FROM note n WHERE n.research_item_id = i.id)"
        sql += " ORDER BY i.posted_at DESC NULLS LAST, i.id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT %s"
            params = (limit,)

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [
                ItemSummary(
                    id=int(row[0]),
                    posted_at=_iso(row[1]),
                    channel=row[2],
                    keywords=tuple(row[6]) if row[6] else (),
                    note_count=int(row[5]),
                    preview=preview(row[3] or "", preview_width),
                    url=row[4],
                )
                for row in cur.fetchall()
            ]

    # -- view --------------------------------------------------------------

    def get_item(self, item_id: int) -> ItemDetail | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT i.id, i.external_id, i.posted_at, i.ingested_at, "
                "COALESCE(s.title, s.platform_id), s.handle, i.original_url, i.raw_text "
                "FROM research_item i JOIN source s ON s.id = i.source_id WHERE i.id = %s",
                (item_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            cur.execute(
                "SELECT name, value, origin FROM structured_field "
                "WHERE research_item_id = %s ORDER BY origin, id",
                (item_id,),
            )
            fields = tuple(Field(name=r[0], value=r[1], origin=r[2]) for r in cur.fetchall())

            cur.execute(
                "SELECT body, author, created_at FROM note "
                "WHERE research_item_id = %s ORDER BY id",
                (item_id,),
            )
            notes = tuple(
                Note(body=r[0], author=r[1], created_at=_iso(r[2]) or "")
                for r in cur.fetchall()
            )

            cur.execute(
                "SELECT kind, file_name, storage_path FROM media_asset "
                "WHERE research_item_id = %s ORDER BY id",
                (item_id,),
            )
            media = tuple(
                Media(kind=r[0], file_name=r[1], storage_path=r[2]) for r in cur.fetchall()
            )

        return ItemDetail(
            id=int(row[0]),
            external_id=row[1],
            posted_at=_iso(row[2]),
            ingested_at=_iso(row[3]) or "",
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
        """Full-text search over collected text (MVP tier: tsvector, no embeddings)."""
        query = query.strip()
        if not query:
            return []
        fn, argument = _tsquery(query)
        if not argument:
            return []

        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                WITH q AS (SELECT {fn}('russian', %s) AS tsq)
                SELECT i.id, i.posted_at, COALESCE(s.title, s.platform_id),
                       ts_headline('russian', i.raw_text, q.tsq,
                                   'StartSel=>>, StopSel=<<, MaxWords=12, MinWords=5')
                FROM research_item i
                JOIN source s ON s.id = i.source_id
                CROSS JOIN q
                WHERE i.fts @@ q.tsq
                ORDER BY ts_rank(i.fts, q.tsq) DESC, i.id DESC
                LIMIT %s
                """,
                (argument, limit),
            )
            return [
                SearchHit(
                    id=int(r[0]),
                    posted_at=_iso(r[1]),
                    channel=r[2],
                    snippet=" ".join((r[3] or "").split()),
                )
                for r in cur.fetchall()
            ]

    # -- stats -------------------------------------------------------------

    def stats(self) -> StoreStats:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT (SELECT COUNT(*) FROM research_item),
                       (SELECT COUNT(*) FROM source),
                       (SELECT COUNT(*) FROM note),
                       (SELECT COUNT(*) FROM media_asset),
                       (SELECT COUNT(*) FROM research_item i WHERE NOT EXISTS
                          (SELECT 1 FROM note n WHERE n.research_item_id = i.id)),
                       (SELECT MIN(posted_at) FROM research_item),
                       (SELECT MAX(posted_at) FROM research_item)
                """
            )
            items, sources, notes, media, unreviewed, first, last = cur.fetchone()

            cur.execute(
                "SELECT value, COUNT(*) c FROM structured_field "
                "WHERE name = 'keyword' GROUP BY value ORDER BY c DESC, value"
            )
            keywords = {r[0]: int(r[1]) for r in cur.fetchall()}

        return StoreStats(
            items=int(items),
            sources=int(sources),
            notes=int(notes),
            media=int(media),
            unreviewed=int(unreviewed),
            keywords=keywords,
            first_post=_iso(first),
            last_post=_iso(last),
        )

    # -- export ------------------------------------------------------------

    def export_rows(self) -> list[dict[str, str]]:
        """Flat table snapshot -- a disposable view, never a second source of truth."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.posted_at, COALESCE(s.title, s.platform_id),
                       (SELECT string_agg(f.value, ', ' ORDER BY f.id)
                          FROM structured_field f
                         WHERE f.research_item_id = i.id AND f.name = 'keyword'),
                       (SELECT COUNT(*) FROM note n WHERE n.research_item_id = i.id),
                       i.original_url, i.raw_text
                FROM research_item i
                JOIN source s ON s.id = i.source_id
                ORDER BY i.posted_at DESC NULLS LAST, i.id DESC
                """
            )
            return [
                {
                    "id": str(r[0]),
                    "posted_at": _iso(r[1]) or "",
                    "channel": r[2],
                    "matched_keywords": r[3] or "",
                    "notes": str(r[4]),
                    "url": r[5] or "",
                    "text": r[6],
                }
                for r in cur.fetchall()
            ]
