"""The seams of the pipeline.

Each protocol is small on purpose. The pipeline depends on these, never on a
concrete Telegram client or a concrete database, so swapping SQLite for
Supabase or adding a YouTube source touches one wiring function.

The storage side is split in four rather than gathered into one `Store`,
because the four have different callers with different rights:

  Store            the collection pipeline. May create items. Runs unattended.
  MachineStore     the model stages. May add derived data, never analysis.
  AnnotationStore  the human, through the review interface. May only annotate.
  ReadStore        the CLI, the review page, search. May not write at all.

That split is Interface Segregation doing real work: the unattended pipeline is
handed an object with no `add_note` on it, so no amount of future code in
`ingest.py` can quietly file machine output as human analysis. The `note`
table's `author = 'human'` CHECK is the second lock on the same door.

`runtime_checkable` only verifies that the *methods exist*, never their
signatures -- it catches a wiring mistake, it is not a type checker.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from .domain import RawPost
from .filtering.gate import Decision
from .llm.protocol import Provenance
from .storage.views import ItemDetail, ItemSummary, SearchHit, StoreStats


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


class Resource(Protocol):
    """Anything holding a connection worth closing."""

    def close(self) -> None:
        ...

    def __enter__(self): ...

    def __exit__(self, *exc) -> None: ...


@runtime_checkable
class Store(Resource, Protocol):
    """Creates research items. Returns False when this source already had one."""

    def save(self, post: RawPost, keywords: tuple[str, ...]) -> bool:
        ...

    def count_items(self) -> int:
        """How many items the store holds, so an ingest run can report a total.

        The one read a writer is allowed. Everything else the pipeline might
        want to know goes through ReadStore.
        """
        ...


@runtime_checkable
class MachineStore(Resource, Protocol):
    """The model stages' writes. Derived data only, always with provenance.

    This is plan §3's two guardrails expressed as an interface:

      * There is no `add_note` here, so a model cannot file a note. Notes are
        the human's analysis and the `author = 'human'` CHECK backs that up.
      * Both methods demand a `Provenance`, so nothing machine-written can
        enter the store anonymously.

    Neither method can touch `raw_text` or downloaded media. Cleaning writes to
    a separate column; extraction writes new rows. The original stays exactly
    as collected, which is what lets the human check the model's work against
    what the model actually saw.
    """

    def set_cleaned_text(
        self, item_id: int, cleaned_text: str, provenance: Provenance
    ) -> None:
        """Record a cleaned copy alongside the raw text, never over it."""
        ...

    def add_machine_field(
        self, item_id: int, name: str, value: str, provenance: Provenance
    ) -> int:
        """Record an extracted fact as origin='system', with its provenance."""
        ...

    def set_simhash(self, item_id: int, value: str) -> None:
        """Record the near-duplicate fingerprint. A flag, never a deletion."""
        ...

    def items_needing(
        self, prompt_version: str, *, limit: int | None = None
    ) -> list[tuple[int, str]]:
        """Items this prompt version has not processed yet.

        The one read a machine writer is allowed, and it exists so a re-run is
        cheap rather than merely safe: without it, every run would pay to
        reprocess the whole store to discover it had nothing to do.
        """
        ...

    def untraceable_system_fields(self) -> int:
        """How many machine fields predate the provenance requirement."""
        ...

    def backfill_keyword_provenance(self) -> int:
        """Attribute pre-existing keyword fields to the gate. Never automatic."""
        ...


@runtime_checkable
class AnnotationStore(Resource, Protocol):
    """The human's writes. Both record provenance as 'human'."""

    def add_note(self, item_id: int, body: str) -> int:
        ...

    def add_field(self, item_id: int, name: str, value: str) -> int:
        ...


@runtime_checkable
class ReadStore(Resource, Protocol):
    """Every read the review interface and search need. Cannot write.

    Backends enforce that structurally where they can -- the SQLite reader
    opens its connection `mode=ro`, so a stray UPDATE raises rather than being
    caught in review.
    """

    def list_items(
        self,
        *,
        limit: int | None = None,
        unreviewed_only: bool = False,
        preview_width: int = 60,
    ) -> list[ItemSummary]:
        ...

    def get_item(self, item_id: int) -> ItemDetail | None:
        ...

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        ...

    def stats(self) -> StoreStats:
        ...

    def export_rows(self) -> list[dict[str, str]]:
        ...
