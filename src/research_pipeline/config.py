"""The composition root: the one place that names a concrete backend.

Everything else in the package depends on the protocols. This module is where
abstraction stops and a real class gets constructed, which is what makes
"swap SQLite for Supabase" a change to one file instead of a search-and-replace
across every script.

Two rules keep it that way:

  * Nothing outside this module imports `sqlite_store`, `supabase_store` or
    `anthropic_client`. If a script needs a store or a model it asks here.
  * The Supabase and Anthropic imports are deliberately *inside* the functions.
    The project declares no required runtime dependencies, and someone running
    the default SQLite backend with no model should never need a Postgres
    driver or an LLM SDK installed to do it.

The model is wired the same way storage is, for the same reason: plan §3 says
the LLM sits behind an interface so it can be disabled per stage, and the place
that decides whether it is disabled is here. With no API key configured, this
hands out `DisabledLLM` and every model stage degrades to its deterministic
half instead of failing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Importing the client *module* is free -- it imports the `anthropic` SDK
# inside its own methods, exactly as supabase_store does with psycopg. Only the
# name of the default model is needed up here.
from .llm.anthropic_client import DEFAULT_MODEL
from .llm.protocol import DisabledLLM, LLMClient
from .protocols import AnnotationStore, MachineStore, ReadStore, Store

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


def open_machine_store(config: StorageConfig) -> MachineStore:
    """The writer the model stages use. Adds derived data, cannot add analysis.

    Same object again, narrower type. A cleaning or extraction stage holding
    this cannot reach `add_note`, so machine output cannot be filed as the
    human's thinking even by accident.
    """
    return open_collection_store(config)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMConfig:
    """Which model to build, or none. Resolved from the environment once."""

    api_key: str | None = None
    model: str = DEFAULT_MODEL
    batch: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def label(self) -> str:
        """What to show a human. Never carries the key."""
        if not self.enabled:
            return "disabled (no ANTHROPIC_API_KEY)"
        return f"{self.model} ({'batch' if self.batch else 'interactive'})"

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """An absent key means disabled, not broken.

        Deliberately not an error. Running the whole pipeline with no model is
        a supported configuration (plan §3's fallback), so the empty key in
        .env.example has to mean "off" rather than "misconfigured".
        """
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if key and not _flag("LLM_ENABLED", True):
            key = ""  # explicitly switched off with the key still on disk

        model = (os.environ.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL
        return cls(
            api_key=key or None,
            model=model,
            # Passive collection is unattended and latency-insensitive, so it
            # goes through the Batch API at half price. Interactive
            # reprocessing sets LLM_BATCH=false.
            batch=_flag("LLM_BATCH", True),
        )


def open_llm_client(config: LLMConfig) -> LLMClient:
    """The model, or a stand-in that skips every task.

    `DisabledLLM` is not an error path. It is the configuration in which the
    deterministic stages run alone, which is what makes "turn the AI off" a
    setting rather than a branch of the codebase.
    """
    if not config.enabled:
        return DisabledLLM()

    from .llm.anthropic_client import AnthropicClient

    return AnthropicClient(
        api_key=config.api_key or "", model=config.model, batch=config.batch
    )
