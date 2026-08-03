"""What a read of the store returns, independent of what stores it.

These types exist so the CLI and the review page never learn a column order,
a dialect, or which backend answered. SQLite and Supabase both assemble the
same shapes, which is what lets the two be swapped without touching a caller.

They live apart from any backend module on purpose: if these were defined in
sqlite_store's neighbourhood, the Supabase reader would have to import the
SQLite implementation to describe its own return type, and the dependency
would point exactly the wrong way.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


def preview(text: str, width: int) -> str:
    """First meaningful line, collapsed to one line and clipped.

    Shared rather than reimplemented per backend: a list view that looked
    different depending on which store answered would be a bug that only ever
    showed up in production.
    """
    for line in text.splitlines():
        stripped = line.strip().strip("*").strip()
        if stripped:
            return stripped[:width] + ("…" if len(stripped) > width else "")
    return ""
