"""Reads posts out of the trial Telegram archive.

The trial tool already downloaded these posts -- unconditionally, which is the
problem this pipeline exists to fix. Treating that archive as a Source lets
the gate be exercised end-to-end against real posts today, without Telegram
credentials, and it is the same shape a live Telegram adapter will have: yield
RawPost, know nothing about filtering or storage.

Read-only. It never writes to the archive.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..domain import RawMedia, RawPost, RawSource

PLATFORM = "telegram"


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class ArchiveSource:
    """Yields every post in the archive, filtered by nobody."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"archive not found: {self.db_path}")

    def fetch(self) -> Iterator[RawPost]:
        conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        try:
            chats = {
                row[0]: row
                for row in conn.execute("SELECT id, title, username FROM chats")
            }
            media_by_message: dict[tuple[int, int], list[RawMedia]] = {}
            for chat_id, msg_id, kind, path, name, size, duration in conn.execute(
                "SELECT chat_id, message_id, type, file_path, file_name, "
                "file_size, duration FROM media WHERE message_id IS NOT NULL"
            ):
                media_by_message.setdefault((chat_id, msg_id), []).append(
                    RawMedia(
                        kind=kind or "unknown",
                        path=path,
                        file_name=name,
                        size_bytes=size,
                        duration=duration,
                    )
                )

            rows = conn.execute(
                "SELECT id, chat_id, date, text FROM messages ORDER BY chat_id, id"
            )
            for msg_id, chat_id, date, text in rows:
                _, title, username = chats.get(chat_id, (chat_id, None, None))
                source = RawSource(
                    platform=PLATFORM,
                    platform_id=str(chat_id),
                    handle=username,
                    title=title,
                )
                url = (
                    f"https://t.me/{username}/{msg_id}"
                    if username
                    else None
                )
                yield RawPost(
                    source=source,
                    external_id=f"{chat_id}:{msg_id}",
                    text=text,
                    posted_at=_parse_date(date),
                    original_url=url,
                    media=tuple(media_by_message.get((chat_id, msg_id), ())),
                )
        finally:
            conn.close()
