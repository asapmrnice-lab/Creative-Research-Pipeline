"""The seams of the pipeline.

Each protocol is small on purpose. The pipeline depends on these, never on a
concrete Telegram client or a concrete database, so swapping SQLite for
Supabase or adding a YouTube source touches one wiring function.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .domain import RawPost
from .filtering.gate import Decision


@runtime_checkable
class Source(Protocol):
    """Produces posts from somewhere. One implementation per source type."""

    def fetch(self) -> Iterable[RawPost]:
        ...


@runtime_checkable
class Gate(Protocol):
    """Decides whether a post is collected at all."""

    def evaluate(self, post: RawPost) -> Decision:
        ...


@runtime_checkable
class Store(Protocol):
    """The only thing allowed to write. Returns False if already stored."""

    def save(self, post: RawPost, keywords: tuple[str, ...]) -> bool:
        ...
