"""The composition root: the one place that names a concrete backend.

Everything else in the package depends on the protocols. This module is where
abstraction stops and a real class gets constructed, which is what makes
"swap SQLite for Supabase" a change to one file instead of a search-and-replace
across every script.

Two rules keep it that way:

  * Nothing outside this module imports `sqlite_store` or `supabase_store`.
    If a script needs a store it asks here.
  * The Supabase imports are deliberately *inside* the functions. The project
    declares no required runtime dependencies, and someone running the default
    SQLite backend should never need a Postgres driver installed to do it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .protocols import AnnotationStore, ReadStore, Store

BACKEND_SQLITE = "sqlite"
BACKEND_SUPABASE = "supabase"
BACKENDS = (BACKEND_SQLITE, BACKEND_SUPABASE)

DEFAULT_DB_PATH = "./data/research.db"


class ConfigError(RuntimeError):
    """Configuration is missing or contradictory. Always actionable."""


def _redact_dsn(dsn: str) -> str:
    """`postgresql://user:secret@host/db` -> `postgresql://user@host/db`.

    The DSN is printed by every script on startup, so the password in it must
    never survive the trip to a terminal, a screenshot or a pasted log.
    """
    scheme, _, rest = dsn.partition("://")
    if not rest or "@" not in rest:
        return dsn
    credentials, _, host = rest.rpartition("@")
    user = credentials.partition(":")[0]
    return f"{scheme}://{user}@{host}" if scheme else f"{user}@{host}"


@dataclass(frozen=True)
class StorageConfig:
    """Which store to build, resolved from the environment exactly once."""

    backend: str
    db_path: Path | None = None
    dsn: str | None = None
    api_url: str | None = None
    service_key: str | None = None

    @property
    def label(self) -> str:
        """What to show a human. Never carries a password or a key."""
        if self.backend == BACKEND_SQLITE:
            return str(self.db_path)
        return f"supabase {_redact_dsn(self.dsn or '')}"

    @classmethod
    def from_env(cls, root: Path, override_db: str | None = None) -> "StorageConfig":
        """Resolve the storage config.

        `override_db` is the scripts' `--db` flag. Passing it forces the SQLite
        backend, because a filesystem path is not a thing Supabase can mean --
        having it silently switch backends would be the surprising behaviour.
        """
        backend = os.environ.get("STORE_BACKEND", BACKEND_SQLITE).strip().casefold()
        if backend not in BACKENDS:
            raise ConfigError(
                f"STORE_BACKEND must be one of {', '.join(BACKENDS)}, got {backend!r}"
            )

        if override_db:
            backend = BACKEND_SQLITE

        if backend == BACKEND_SQLITE:
            raw = override_db or os.environ.get("STORE_DB_PATH", DEFAULT_DB_PATH)
            path = Path(raw)
            return cls(backend=backend, db_path=path if path.is_absolute() else root / path)

        dsn = (os.environ.get("SUPABASE_DB_URL") or "").strip()
        if not dsn:
            raise ConfigError(
                "STORE_BACKEND=supabase but SUPABASE_DB_URL is empty. Copy the "
                "connection string from your Supabase project "
                "(Settings -> Database -> Connection string -> URI) into .env, "
                "or set STORE_BACKEND=sqlite to keep working locally."
            )
        # Not required to store anything: these are for the Storage bucket that
        # media uploads will need later. Absent is fine until then.
        return cls(
            backend=backend,
            dsn=dsn,
            api_url=(os.environ.get("SUPABASE_URL") or "").strip() or None,
            service_key=(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None,
        )


def open_read_store(config: StorageConfig) -> ReadStore:
    """A read-only view of the store."""
    if config.backend == BACKEND_SQLITE:
        from .storage.reader import ResearchStoreReader

        return ResearchStoreReader(config.db_path)

    from .storage.supabase_store import SupabaseReadStore

    return SupabaseReadStore(config.dsn)


def open_collection_store(config: StorageConfig) -> Store:
    """The writer the unattended pipeline uses. Creates items, cannot annotate."""
    if config.backend == BACKEND_SQLITE:
        from .storage.sqlite_store import SqliteStore

        return SqliteStore(config.db_path)

    from .storage.supabase_store import SupabaseStore

    return SupabaseStore(config.dsn)


def open_annotation_store(config: StorageConfig) -> AnnotationStore:
    """The writer the human uses. Same object as the collection store today.

    They are separate functions because they are separate *rights*, not because
    they are separate classes -- the type each returns is what constrains the
    caller. If the two ever need to diverge, they already can.
    """
    return open_collection_store(config)  # type: ignore[return-value]
