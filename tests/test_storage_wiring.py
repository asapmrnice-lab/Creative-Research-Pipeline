"""Prove the storage seam holds, without needing a Postgres to prove it.

Three things are worth catching here.

First, that the two backends can drift apart. The whole SQLite/Supabase swap
rests on both satisfying the same protocols, and nothing enforces that at
import time. `issubclass` against a method-only Protocol does enforce it, and
costs no database -- which means a Supabase method renamed or dropped fails
here rather than the first time someone runs against the real project.

Second, that a password could leak. The DSN is printed on every script start,
so `_redact_dsn` is a security property, not a formatting nicety.

Third, that the config could silently pick the wrong backend, which is the
failure mode where you think you are writing to Supabase and are not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_pipeline.config import (
    BACKEND_SQLITE,
    BACKEND_SUPABASE,
    ConfigError,
    StorageConfig,
    _redact_dsn,
    open_collection_store,
    open_read_store,
)
from research_pipeline.protocols import AnnotationStore, ReadStore, Store
from research_pipeline.storage.reader import ResearchStoreReader
from research_pipeline.storage.sqlite_store import SqliteStore

# Imported for the conformance check only. Importing the module must NOT
# require psycopg -- the driver is imported inside the connect helper, so that
# the default backend never pays for a dependency it does not use.
from research_pipeline.storage.supabase_store import (
    SupabaseReadStore,
    SupabaseStore,
    _iso,
    _tsquery,
)

DSN = "postgresql://postgres.abcd:sup3r-s3cret@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Config must be read from the environment, not inherited from the shell."""
    for key in (
        "STORE_BACKEND",
        "STORE_DB_PATH",
        "SUPABASE_DB_URL",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


# -- both backends implement the same protocols ----------------------------


@pytest.mark.parametrize(
    "implementation, protocol",
    [
        (SqliteStore, Store),
        (SupabaseStore, Store),
        (SqliteStore, AnnotationStore),
        (SupabaseStore, AnnotationStore),
        (ResearchStoreReader, ReadStore),
        (SupabaseReadStore, ReadStore),
    ],
)
def test_backends_satisfy_their_protocols(implementation, protocol):
    assert issubclass(implementation, protocol)


def test_the_conformance_check_can_actually_fail():
    """A test that always passes proves nothing. This shows the check has teeth."""

    class Forgetful:
        def save(self, post, keywords):  # has save, but no count_items
            return True

    assert not issubclass(Forgetful, Store)


def test_conformance_covers_the_inherited_resource_methods():
    """The interesting case: a backend that works but leaks connections.

    `close`/`__enter__`/`__exit__` come from `Resource`, one level up. If
    inheritance did not carry them into the check, a store that never closed
    its connection would pass conformance and fail in production.
    """

    class NeverCloses:
        def save(self, post, keywords):
            return True

        def count_items(self) -> int:
            return 0

    assert not issubclass(NeverCloses, Store)


# -- secrets never reach a terminal ----------------------------------------


def test_redact_dsn_removes_the_password():
    redacted = _redact_dsn(DSN)
    assert "sup3r-s3cret" not in redacted
    assert "postgres.abcd" in redacted
    assert "aws-0-eu-west-1.pooler.supabase.com:5432/postgres" in redacted


def test_label_never_carries_the_password():
    config = StorageConfig(backend=BACKEND_SUPABASE, dsn=DSN)
    assert "sup3r-s3cret" not in config.label


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://host/db", "", "not-a-dsn", "postgresql://user@host/db"],
)
def test_redact_dsn_survives_input_without_a_password(dsn):
    _redact_dsn(dsn)  # must not raise


# -- backend selection ------------------------------------------------------


def test_defaults_to_sqlite(tmp_path, monkeypatch):
    config = StorageConfig.from_env(tmp_path)
    assert config.backend == BACKEND_SQLITE
    assert config.db_path == tmp_path / "data" / "research.db"


def test_relative_store_path_resolves_against_the_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DB_PATH", "./somewhere/other.db")
    config = StorageConfig.from_env(tmp_path)
    assert config.db_path == tmp_path / "somewhere" / "other.db"


def test_absolute_store_path_is_left_alone(tmp_path, monkeypatch):
    absolute = (tmp_path / "elsewhere.db").resolve()
    monkeypatch.setenv("STORE_DB_PATH", str(absolute))
    assert StorageConfig.from_env(tmp_path).db_path == absolute


def test_supabase_backend_reads_the_dsn(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", DSN)
    config = StorageConfig.from_env(tmp_path)
    assert config.backend == BACKEND_SUPABASE
    assert config.dsn == DSN


def test_supabase_without_a_dsn_fails_with_an_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    with pytest.raises(ConfigError) as exc:
        StorageConfig.from_env(tmp_path)
    message = str(exc.value)
    assert "SUPABASE_DB_URL" in message
    assert "STORE_BACKEND=sqlite" in message  # tells you how to keep working


def test_supabase_does_not_require_the_rest_credentials(tmp_path, monkeypatch):
    """Those are for the media bucket, which does not exist yet."""
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", DSN)
    config = StorageConfig.from_env(tmp_path)
    assert config.api_url is None and config.service_key is None


def test_unknown_backend_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_BACKEND", "mysql")
    with pytest.raises(ConfigError, match="sqlite, supabase"):
        StorageConfig.from_env(tmp_path)


def test_db_override_forces_sqlite(tmp_path, monkeypatch):
    """--db is a file path, so it cannot mean Supabase.

    Silently connecting to Supabase while the human passed a local file would
    be the worst kind of wrong: it looks like it worked.
    """
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_DB_URL", DSN)
    config = StorageConfig.from_env(tmp_path, override_db="local.db")
    assert config.backend == BACKEND_SQLITE
    assert config.db_path == tmp_path / "local.db"


# -- the composition root builds what it says ------------------------------


def test_composition_root_builds_real_sqlite_objects(tmp_path):
    config = StorageConfig(backend=BACKEND_SQLITE, db_path=tmp_path / "research.db")
    with open_collection_store(config) as store:
        assert isinstance(store, SqliteStore)
    with open_read_store(config) as reader:
        assert isinstance(reader, ResearchStoreReader)


def test_supabase_is_never_constructed_for_a_sqlite_config(tmp_path, monkeypatch):
    """Guards the lazy import: choosing sqlite must not touch psycopg."""
    import research_pipeline.storage.supabase_store as supa

    def explode(*args, **kwargs):
        raise AssertionError("sqlite config must not open a Postgres connection")

    monkeypatch.setattr(supa, "_connect", explode)
    config = StorageConfig(backend=BACKEND_SQLITE, db_path=tmp_path / "research.db")
    with open_collection_store(config):
        pass


# -- Postgres query building, which is pure and worth pinning --------------


def test_bare_words_become_prefix_queries():
    """Mirrors the SQLite reader: searching "крео" must find "креативы"."""
    fn, argument = _tsquery("крео кейс")
    assert fn == "to_tsquery"
    assert argument == "крео:* & кейс:*"


def test_tsquery_operators_are_stripped_from_bare_words():
    """A post is not a query language; an unbalanced paren must not 500."""
    _, argument = _tsquery("кейс(!)")
    assert "(" not in argument and "!" not in argument
    assert argument == "кейс:*"


@pytest.mark.parametrize("query", ['"точная фраза"', "крео OR кейс", "крео -реклама"])
def test_search_syntax_is_handed_to_websearch(query):
    fn, argument = _tsquery(query)
    assert fn == "websearch_to_tsquery"
    assert argument == query


def test_tsquery_of_only_punctuation_is_empty_not_broken():
    _, argument = _tsquery("((()))")
    assert argument == ""


def test_iso_renders_dates_the_same_way_sqlite_stores_them():
    """The CLI must not format dates differently depending on the backend."""
    moment = datetime(2026, 7, 30, 15, 57, 39, tzinfo=timezone.utc)
    assert _iso(moment).startswith("2026-07-30T15:57:39")
    assert _iso(None) is None
    assert _iso("2026-07-30T15:57:39") == "2026-07-30T15:57:39"
