"""Where Claude is allowed to operate -- expressed as types.

Plan §3 permits the model four mechanical transforms and forbids every
judgment. That boundary is only worth anything if it is enforced somewhere
other than a table in a document, so it is enforced here, in three ways:

  * **Every call returns JSON against a schema the caller wrote.** There is no
    "give me prose" method on this protocol. A stage cannot ask the model for
    an opinion because there is no shape for one to come back in.
  * **Every machine output carries `Provenance`.** A result the human cannot
    trace to a model and a prompt version is not a result, it is a rumour --
    so `Provenance` refuses to be constructed without both.
  * **Absent is a real answer.** `DisabledLLM` skips every task instead of
    raising, which is what makes §3's fallback ("regex/heuristic cleaning with
    no model at all") a supported configuration rather than a rewrite.

Nothing here imports `anthropic`. The pipeline depends on this module; only
the composition root names a concrete client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

# The Batches API accepts this alphabet for a request's custom_id, and the key
# is reused verbatim as that id. Enforced at construction rather than at send
# time so a bad key fails in a unit test, not against a paid endpoint.
_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class LLMError(RuntimeError):
    """The model layer is misconfigured. Always actionable."""


@dataclass(frozen=True)
class Provenance:
    """Who produced a machine output, and under which instructions.

    Stage 2 gives every Structured Field an `origin`; plan §3 extends that with
    the model and the prompt version, so a machine claim can be disproved --
    you can see what produced it and re-run exactly that.

    `confidence` is optional because not every transform has one. Cleaning
    either removed a span or it did not.
    """

    model: str
    prompt_version: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("provenance needs a model -- machine output is traceable")
        if not self.prompt_version.strip():
            raise ValueError(
                "provenance needs a prompt_version -- output that cannot be "
                "re-run cannot be checked"
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within 0..1, got {self.confidence}")


@dataclass(frozen=True)
class JsonTask:
    """One structured request about one post.

    Split in two on purpose. `instructions` is the stable part -- the whitelist,
    the cleaning rules -- and is what gets a cache breakpoint. `payload` is the
    post, and changes every time, so it must come after that breakpoint or the
    prefix never matches (see AnthropicClient for what that costs).
    """

    key: str
    instructions: str
    payload: str
    schema: dict[str, Any]
    max_tokens: int = 1024

    def __post_init__(self) -> None:
        if not _KEY.match(self.key):
            raise ValueError(
                f"task key {self.key!r} must be 1-64 chars of [A-Za-z0-9_-]: it is "
                "sent as the batch request's custom_id"
            )
        if not self.instructions.strip():
            raise ValueError("a task needs instructions")


@dataclass(frozen=True)
class JsonResult:
    """What came back, including the two ways it can come back with no data."""

    key: str
    data: dict[str, Any] | None = None
    model: str | None = None
    error: str | None = None
    # Reported rather than assumed: whether the instructions actually cached is
    # a property of the model and the prompt length, not of asking for it.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def skipped(self) -> bool:
        """No model ran, and that is not a failure.

        A disabled stage returns this. Callers keep their deterministic output
        and write nothing, rather than erroring or -- worse -- writing a blank.
        """
        return self.data is None and self.error is None


@dataclass
class Usage:
    """What a run cost, so caching can be verified instead of hoped for."""

    calls: int = 0
    failures: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def record(self, result: JsonResult) -> None:
        self.calls += 1
        if result.error:
            self.failures += 1
        self.cache_read_tokens += result.cache_read_tokens
        self.cache_write_tokens += result.cache_write_tokens

    @property
    def cache_hit(self) -> bool:
        return self.cache_read_tokens > 0


@runtime_checkable
class LLMClient(Protocol):
    """A structured-JSON transform. The only thing a stage may ask a model for."""

    @property
    def model(self) -> str:
        ...

    @property
    def enabled(self) -> bool:
        """Whether this client will actually do work.

        A stage needs this to name its own prompt version honestly. A cleaner
        running without a model produces deterministic output and must say so,
        or a re-run would look for a version that was never stored and redo
        every item forever. It is also what lets enabling the model later
        reprocess exactly the items that never saw one.
        """
        ...

    def complete_json(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        """One result per task, in the order the tasks were given."""
        ...


@dataclass
class DisabledLLM:
    """No model. Every task is skipped and the caller carries on.

    This is the configuration plan §3 offers as the fallback if the Allowed
    column still feels like too much AI: deterministic cleaning and exact
    dedup still run, and nothing machine-written is stored at all.
    """

    reason: str = "no ANTHROPIC_API_KEY configured"

    @property
    def model(self) -> str:
        return "disabled"

    @property
    def enabled(self) -> bool:
        return False

    def complete_json(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        return [JsonResult(key=task.key) for task in tasks]


@dataclass
class RecordingLLM:
    """A scripted client, for tests and dry runs.

    Lives beside the protocol rather than in the test suite because every stage
    needs it and none of them should each grow their own.
    """

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_name: str = "test-model"
    seen: list[JsonTask] = field(default_factory=list)

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def enabled(self) -> bool:
        return True

    def complete_json(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        results = []
        for task in tasks:
            self.seen.append(task)
            data = self.responses.get(task.key)
            results.append(
                JsonResult(key=task.key, data=data, model=self.model_name)
                if data is not None
                else JsonResult(key=task.key, error="no scripted response")
            )
        return results
