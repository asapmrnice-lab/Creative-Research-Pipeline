"""The objects that move through the pipeline.

Deliberately plain data: no source knows how these are stored, and storage
knows nothing about Telegram. A new source type produces RawPost and nothing
downstream changes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

# Collapse runs of whitespace before hashing so a repost that only differs in
# line breaks hashes identically.
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class RawMedia:
    """A file attached to a post, as the source describes it."""

    kind: str
    path: str | None = None
    original_url: str | None = None
    file_name: str | None = None
    size_bytes: int | None = None
    duration: int | None = None


@dataclass(frozen=True)
class RawSource:
    """Where a post came from."""

    platform: str
    platform_id: str
    handle: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class RawPost:
    """One post from one source, before any decision has been made about it."""

    source: RawSource
    external_id: str
    text: str | None
    posted_at: datetime | None = None
    original_url: str | None = None
    media: tuple[RawMedia, ...] = field(default_factory=tuple)

    def content_hash(self) -> str:
        """Identity for exact-duplicate detection.

        Scoped to the source, so the same text posted in two channels stays
        two research items -- they are two separate observations.
        """
        normalized = _WHITESPACE.sub(" ", (self.text or "")).strip().casefold()
        payload = f"{self.source.platform}:{self.source.platform_id}:{normalized}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
