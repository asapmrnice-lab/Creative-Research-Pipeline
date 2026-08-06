"""The edge where plan §3 meets the API, tested without touching the API.

`test_machine_stages.py` proves what each stage may *ask* for. This proves what
happens to what comes *back*, and who decides whether a model runs at all --
the two places where §3's guarantees could still be lost after every stage has
behaved:

  * a response is believed only after it parses into an object; a truncated or
    prose answer is reported, never half-stored
  * a batch result is filed by its `custom_id` and never by its position, so a
    fact cannot land on the wrong post
  * a misconfiguration stops the run; one bad post does not
  * cache tokens are reported as measured, not assumed (plan §3a, point 4)
  * with no key configured the composition root hands out `DisabledLLM`, which
    is the default and the whole of §3's model-free fallback

The SDK is never imported. `AnthropicClient` takes its connection as a field,
so every test here injects a stand-in and runs offline in milliseconds.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_pipeline.cleaning.llm_cleaner import LlmCleaner
from research_pipeline.config import LLMConfig, open_llm_client
from research_pipeline.detect import detect
from research_pipeline.detect.signals import DETECT_VERSION
from research_pipeline.domain import RawPost, RawSource
from research_pipeline.extraction import ExtractionEngine
from research_pipeline.llm.anthropic_client import DEFAULT_MODEL, AnthropicClient
from research_pipeline.llm.protocol import (
    DisabledLLM,
    JsonTask,
    LLMClient,
    LLMError,
    Provenance,
    Usage,
)
from research_pipeline.storage.sqlite_store import SqliteStore

MODEL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "LLM_ENABLED", "LLM_BATCH")


@pytest.fixture
def clean_env(monkeypatch):
    """No inherited configuration.

    These tests are about what the environment *says*, so a key in the
    developer's own .env must not decide the answer.
    """
    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def task(key: str = "k1") -> JsonTask:
    return JsonTask(key=key, instructions="extract", payload="кейс", schema={})


# -- a stand-in for the SDK -------------------------------------------------
# Shaped like the response objects the client reads, and nothing else: it has
# `.content`, `.usage`, `.custom_id`, `.result.type`. If the client ever starts
# reading a field the real SDK has and this does not, these tests fail rather
# than pass on a mock that agrees with everything.


def message(payload, *, cache_read: int = 0, cache_write: int = 0) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write
        ),
    )


class FakeMessages:
    """The sync endpoint. Replies are consumed in order; an Exception raises."""

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeBatches:
    def __init__(self, entries, statuses=("ended",)) -> None:
        self._entries = list(entries)
        self._statuses = list(statuses)
        self.submitted: list[dict] | None = None
        self.retrieves = 0

    def create(self, requests):
        self.submitted = list(requests)
        return SimpleNamespace(id="msgbatch_test")

    def retrieve(self, batch_id):
        self.retrieves += 1
        status = self._statuses[min(self.retrieves - 1, len(self._statuses) - 1)]
        return SimpleNamespace(id=batch_id, processing_status=status)

    def results(self, batch_id):
        return iter(self._entries)


def entry(custom_id: str, payload=None, outcome: str = "succeeded") -> SimpleNamespace:
    result = SimpleNamespace(type=outcome)
    if outcome == "succeeded":
        result.message = message(payload)
    return SimpleNamespace(custom_id=custom_id, result=result)


def sync_client(replies, **kwargs) -> tuple[AnthropicClient, FakeMessages]:
    messages = FakeMessages(replies)
    client = AnthropicClient(
        api_key="test-key",
        batch=False,
        _client=SimpleNamespace(messages=messages),
        **kwargs,
    )
    return client, messages


def batch_client(entries, statuses=("ended",), **kwargs):
    batches = FakeBatches(entries, statuses)
    client = AnthropicClient(
        api_key="test-key",
        batch=True,
        poll_seconds=0,
        _client=SimpleNamespace(messages=SimpleNamespace(batches=batches)),
        **kwargs,
    )
    return client, batches


# ===========================================================================
# What comes back: the interactive path
# ===========================================================================


def test_a_structured_answer_becomes_data_and_names_the_model():
    client, _ = sync_client([message({"geo": "Бразилия", "confidence": 0.8})])
    result = client.complete_json([task()])[0]

    assert result.ok and not result.skipped
    assert result.data == {"geo": "Бразилия", "confidence": 0.8}
    assert result.model == DEFAULT_MODEL
    assert result.key == "k1"  # the caller's key, so it can be filed


def test_a_response_cut_short_is_reported_rather_than_half_stored():
    """`output_config.format` guarantees valid JSON -- until max_tokens ends it.

    A truncated object is the one way a schema-constrained response still comes
    back unusable, and storing half of it would store a fact the post does not
    contain.
    """
    client, _ = sync_client([message('{"geo": "Браз')])
    result = client.complete_json([task()])[0]

    assert result.data is None
    assert "unparseable" in (result.error or "")
    assert not result.skipped  # a failure, not an absent model


def test_an_answer_that_is_not_an_object_is_refused():
    """A bare list or string has no field names, so nothing can be filed."""
    client, _ = sync_client([message('["Бразилия"]')])
    assert "not an object" in (client.complete_json([task()])[0].error or "")


def test_one_bad_post_fails_alone_and_the_run_carries_on():
    """The backlog is thousands of posts; one refusal must not end the run."""

    class OverloadedError(Exception):
        pass

    client, _ = sync_client(
        [OverloadedError("try again later"), message({"geo": "RU", "confidence": 1})]
    )
    first, second = client.complete_json([task("k1"), task("k2")])

    assert "OverloadedError" in (first.error or "") and first.data is None
    assert second.data == {"geo": "RU", "confidence": 1}


def test_a_misconfiguration_stops_the_run_instead_of_burning_the_backlog():
    """A bad key fails every remaining post identically and costs money doing it.

    Classified by exception name, so the check is exercised here exactly as it
    is in production -- the client never imports the SDK's exception types.
    """

    class AuthenticationError(Exception):
        pass

    client, messages = sync_client(
        [AuthenticationError("invalid x-api-key"), message({"geo": "RU"})]
    )
    with pytest.raises(LLMError, match="AuthenticationError"):
        client.complete_json([task("k1"), task("k2")])

    assert len(messages.calls) == 1, "the second post was still attempted"


def test_cache_tokens_are_reported_as_measured_never_assumed():
    """Plan §3a, point 4: on Haiku the whitelist prefix is under the minimum.

    Caching then no-ops silently -- no error, just zeros -- so the client
    reports what the API charged and `Usage.cache_hit` is how a run finds out,
    rather than the plan's §5 assumption that the saving happened.
    """
    client, _ = sync_client([message({"geo": "RU"}, cache_read=0, cache_write=0)])
    usage = Usage()
    usage.record(client.complete_json([task()])[0])

    assert usage.calls == 1 and usage.failures == 0
    assert not usage.cache_hit


def test_a_cache_hit_is_reported_the_same_way():
    client, _ = sync_client([message({"geo": "RU"}, cache_read=4200)])
    usage = Usage()
    usage.record(client.complete_json([task()])[0])

    assert usage.cache_read_tokens == 4200 and usage.cache_hit


def test_a_failure_is_counted_as_one():
    usage = Usage()
    client, _ = sync_client([message("not json")])
    usage.record(client.complete_json([task()])[0])
    assert usage.calls == 1 and usage.failures == 1


# ===========================================================================
# What comes back: the batch path (passive collection)
# ===========================================================================


def test_a_batch_result_is_filed_by_its_key_and_never_by_its_position():
    """The worst failure this system could have, and the cheapest to cause.

    Batch results come back in arbitrary order. Matching them positionally
    would attribute one post's facts to another post -- silently, permanently,
    and in the record that exists to be trustworthy. So the entries here are
    returned reversed, and each answer must still find its own question.
    """
    client, _ = batch_client(
        [entry("k2", {"geo": "Аргентина"}), entry("k1", {"geo": "Бразилия"})]
    )
    first, second = client.complete_json([task("k1"), task("k2")])

    assert (first.key, first.data) == ("k1", {"geo": "Бразилия"})
    assert (second.key, second.data) == ("k2", {"geo": "Аргентина"})


def test_a_task_with_no_result_is_reported_rather_than_dropped():
    """A missing answer must be visible; a silent gap looks like an empty post."""
    client, _ = batch_client([entry("k1", {"geo": "RU"})])
    first, second = client.complete_json([task("k1"), task("k2")])

    assert first.ok
    assert second.data is None and "no result" in (second.error or "")
    assert not second.skipped


@pytest.mark.parametrize("outcome", ["errored", "expired", "canceled"])
def test_an_unsuccessful_batch_entry_becomes_a_rerunnable_error(outcome):
    """Re-running is safe: the key is derived from (item, chunk, version)."""
    client, _ = batch_client([entry("k1", outcome=outcome)])
    result = client.complete_json([task("k1")])[0]
    assert result.data is None and outcome in (result.error or "")


def test_every_task_is_submitted_once_under_its_own_custom_id():
    client, batches = batch_client([entry("k1", {}), entry("k2", {})])
    client.complete_json([task("k1"), task("k2")])

    assert [r["custom_id"] for r in batches.submitted or []] == ["k1", "k2"]
    assert all("output_config" in r["params"] for r in batches.submitted or [])


def test_the_run_waits_for_the_batch_to_end():
    client, batches = batch_client(
        [entry("k1", {"geo": "RU"})], statuses=("in_progress", "in_progress", "ended")
    )
    assert client.complete_json([task("k1")])[0].data == {"geo": "RU"}
    assert batches.retrieves == 3


def test_a_batch_that_never_ends_stops_with_a_recoverable_message():
    """Results stay retrievable for 29 days, so the run may end -- not the work."""
    client, _ = batch_client(
        [entry("k1", {})], statuses=("in_progress",), timeout_seconds=0
    )
    with pytest.raises(LLMError, match="re-run to collect them"):
        client.complete_json([task("k1")])


def test_a_submission_that_fails_says_so_rather_than_returning_nothing():
    """An empty result list would read as "no posts needed a model"."""

    class ApiError(Exception):
        pass

    class Broken:
        def create(self, requests):
            raise ApiError("service unavailable")

    client = AnthropicClient(
        api_key="test-key",
        _client=SimpleNamespace(messages=SimpleNamespace(batches=Broken())),
    )
    with pytest.raises(LLMError, match="could not submit batch"):
        client.complete_json([task()])


# ===========================================================================
# The composition root: who decides whether a model runs
# ===========================================================================


def test_no_key_configured_yields_the_client_that_skips_everything(clean_env):
    """§3's fallback is the default, not an escape hatch."""
    config = LLMConfig.from_env()
    assert not config.enabled

    client = open_llm_client(config)
    assert isinstance(client, DisabledLLM)
    assert isinstance(client, LLMClient)
    assert not client.enabled


def test_a_key_on_disk_can_be_switched_off_without_being_deleted(clean_env):
    """Turning the AI off is a setting, so it must not mean editing secrets."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    clean_env.setenv("LLM_ENABLED", "false")

    config = LLMConfig.from_env()
    assert not config.enabled
    assert isinstance(open_llm_client(config), DisabledLLM)


def test_a_configured_key_builds_the_real_client(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    client = open_llm_client(LLMConfig.from_env())

    assert isinstance(client, AnthropicClient) and client.enabled
    assert isinstance(client, LLMClient)


def test_the_default_model_is_haiku_and_the_escape_hatch_is_configuration(clean_env):
    """Plan §5: Haiku by default, Sonnet reachable without a code change."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    assert LLMConfig.from_env().model == DEFAULT_MODEL == "claude-haiku-4-5"

    clean_env.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    assert open_llm_client(LLMConfig.from_env()).model == "claude-sonnet-5"


def test_passive_collection_defaults_to_the_batch_api(clean_env):
    """Half price for the unattended workflow; interactive work opts out."""
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    assert LLMConfig.from_env().batch is True

    clean_env.setenv("LLM_BATCH", "false")
    assert LLMConfig.from_env().batch is False


@pytest.mark.parametrize("label_env", [{}, {"ANTHROPIC_API_KEY": "sk-ant-secret-key"}])
def test_the_printed_label_never_carries_the_key(clean_env, label_env):
    """Every script prints this line on startup, into terminals and screenshots."""
    for name, value in label_env.items():
        clean_env.setenv(name, value)
    assert "secret" not in LLMConfig.from_env().label


# ===========================================================================
# The default configuration, end to end
# ===========================================================================


def test_the_default_configuration_stores_nothing_a_model_produced(tmp_path: Path):
    """The claim in plan §3a, checked rather than asserted in prose.

    Three of §3's four rows run with no key configured, the fourth reports
    itself skipped, and every row written is attributable to code that can be
    read. This is the shape of `scripts/enrich.py` with `ANTHROPIC_API_KEY`
    empty -- the same stages, wired the same way.
    """
    with SqliteStore(tmp_path / "research.db") as store:
        post = RawPost(
            source=RawSource(platform="telegram", platform_id="chat", title="T"),
            external_id="chat:1",
            text="Кейс​ по   Бразилии\nROI 140%, бюджет $5000\nhttps://ex.com/a?utm_source=tg",
        )
        store.save(post, ("кейс",))
        item = int(store._conn.execute("SELECT id FROM research_item").fetchone()[0])

        client = open_llm_client(LLMConfig(api_key=None))
        pending = store.items_needing(LlmCleaner(client).version)
        outcome = LlmCleaner(client).run(pending, store)[0]

        for name, value in detect(outcome.text).as_fields():
            store.add_machine_field(item, name, value, Provenance("detect", DETECT_VERSION))

        report = ExtractionEngine(client).run([(item, outcome.text)], store)

        # Row 1 cleaned, row 3 fingerprinted, row 4 labelled; row 2 stood down.
        assert outcome.text and not outcome.used_model
        assert report.skipped == 1 and report.fields_written == 0

        producers = {
            row[0]
            for row in store._conn.execute(
                "SELECT DISTINCT model FROM structured_field WHERE origin = 'system'"
            )
        }
        assert producers == {"keyword-gate", "detect"}
        assert store.untraceable_system_fields() == 0
        # And the post as collected is still exactly the post as collected.
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE research_item SET raw_text = 'x' WHERE id = ?", (item,)
            )
