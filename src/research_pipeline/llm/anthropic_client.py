"""The concrete model client: Claude Haiku, structured output, batched.

Plan §5 chose the simplest tier that works, and this module is what that means
in code. Every call is a single stateless request with a JSON schema attached.
There is no tool loop, no agent, and no conversation -- a post goes in, a
whitelist-shaped object comes out, and nothing is carried between calls.

Three API details are load-bearing and easy to get wrong:

`output_config.format` rather than prose parsing.
    The whitelist *is* the schema. With `additionalProperties: false` the model
    cannot invent a field, which is what makes "whitelist extraction" a
    structural claim rather than a hopeful instruction. (The older top-level
    `output_format` parameter is deprecated; it is not what this sends.)

The cache breakpoint sits on the instructions, and may not fire.
    Plan §5 assumed caching the whitelist would make passive runs pay ~0.1x on
    the instructions. That holds only above a per-model minimum prefix, and on
    `claude-haiku-4-5` that minimum is 4096 tokens -- well above a short
    whitelist. Under it, caching silently does nothing: no error, just
    `cache_creation_input_tokens: 0`. So this client *reports* cache tokens on
    every result instead of assuming the saving, and `Usage.cache_hit` is how
    you find out. Lengthening the instructions past the minimum to force a hit
    would be paying for tokens to pretend to save tokens; the honest fix is to
    know it is not caching and let the Batch discount do the work.

No `thinking`, no `effort`.
    These are mechanical transforms. `effort` is rejected outright by Haiku
    4.5, and thinking would spend tokens deliberating about text the human
    could have cleaned by hand.

`anthropic` is imported inside the methods, so the default configuration --
which calls no model at all -- never needs the package installed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .protocol import JsonResult, JsonTask, LLMError

DEFAULT_MODEL = "claude-haiku-4-5"

# Batches are unattended, so a run can wait; it cannot wait forever.
DEFAULT_POLL_SECONDS = 20.0
DEFAULT_TIMEOUT_SECONDS = 24 * 3600.0

# Misconfiguration, not a bad post. One of these means every remaining task
# would fail the same way, so the run stops instead of burning through the
# backlog recording the same error 3000 times.
_FATAL = frozenset(
    {
        "AuthenticationError",
        "PermissionDeniedError",
        "NotFoundError",
        "BadRequestError",
    }
)


def _is_fatal(exc: BaseException) -> bool:
    """Classify by class name, so the exception types need no import here."""
    return type(exc).__name__ in _FATAL


def _request_params(task: JsonTask, model: str) -> dict[str, Any]:
    """The body of one request, shared by the sync and batch paths.

    Built in one place because the two paths must stay identical: a batch run
    that shaped its prompt differently from the interactive one would produce
    a store whose contents depended on which workflow happened to write them.
    """
    return {
        "model": model,
        "max_tokens": task.max_tokens,
        "system": [
            {
                "type": "text",
                "text": task.instructions,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        # The post goes after the breakpoint. It changes every call, so
        # anything cached would be invalidated by it.
        "messages": [{"role": "user", "content": task.payload}],
        "output_config": {
            "format": {"type": "json_schema", "schema": task.schema}
        },
    }


def _parse(text: str, key: str, model: str, usage: Any) -> JsonResult:
    """Turn one successful response into a result.

    `output_config.format` guarantees the text parses, but a response cut short
    by `max_tokens` is still truncated JSON -- so this reports a parse failure
    rather than trusting the guarantee.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return JsonResult(key=key, error=f"unparseable response: {exc}", model=model)
    if not isinstance(data, dict):
        return JsonResult(key=key, error="response was not an object", model=model)
    return JsonResult(
        key=key,
        data=data,
        model=model,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _first_text(content) -> str:
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


@dataclass
class AnthropicClient:
    """Haiku by default, sync or batched, injected at the composition root.

    `batch=True` is Workflow 1 (passive collection): unattended, latency
    insensitive, and half price. `batch=False` is interactive reprocessing,
    where a human is waiting.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    batch: bool = True
    poll_seconds: float = DEFAULT_POLL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    _client: Any = field(default=None, repr=False, compare=False)

    @property
    def enabled(self) -> bool:
        """A constructed client always runs; an absent key yields DisabledLLM."""
        return True

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise LLMError(
                "AnthropicClient needs an API key. Set ANTHROPIC_API_KEY in .env, "
                "or leave it empty to run with the model disabled -- deterministic "
                "cleaning and exact dedup work without it."
            )

    # -- connection ---------------------------------------------------------

    def _sdk(self):
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:  # pragma: no cover - install-dependent
                raise LLMError(
                    "the model stages need the anthropic SDK -- install it with\n"
                    '    pip install -e ".[llm]"\n'
                    "or leave ANTHROPIC_API_KEY empty to run without a model."
                ) from exc
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def close(self) -> None:
        self._client = None

    def __enter__(self) -> "AnthropicClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the one thing a stage may ask for ----------------------------------

    def complete_json(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        if not tasks:
            return []
        keys = [t.key for t in tasks]
        if len(set(keys)) != len(keys):
            raise LLMError(
                "task keys must be unique within a call -- they are the batch "
                "custom_ids, and a duplicate would silently drop a post"
            )
        return self._run_batch(tasks) if self.batch else self._run_sync(tasks)

    # -- interactive --------------------------------------------------------

    def _run_sync(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        client = self._sdk()
        results: list[JsonResult] = []
        for task in tasks:
            try:
                response = client.messages.create(**_request_params(task, self.model))
            except Exception as exc:  # noqa: BLE001 - re-raised when fatal
                if _is_fatal(exc):
                    raise LLMError(f"{type(exc).__name__}: {exc}") from exc
                results.append(
                    JsonResult(key=task.key, error=f"{type(exc).__name__}: {exc}")
                )
                continue
            results.append(
                _parse(_first_text(response.content), task.key, self.model, response.usage)
            )
        return results

    # -- passive collection -------------------------------------------------

    def _run_batch(self, tasks: Sequence[JsonTask]) -> list[JsonResult]:
        """Submit every task at once, wait, then reassemble by key.

        Results come back in arbitrary order, so they are keyed by `custom_id`
        and never by position -- matching on position would misfile facts onto
        the wrong posts, which is the worst failure this system could have.
        """
        client = self._sdk()
        try:
            batch = client.messages.batches.create(
                requests=[
                    {"custom_id": task.key, "params": _request_params(task, self.model)}
                    for task in tasks
                ]
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"could not submit batch: {type(exc).__name__}: {exc}") from exc

        self._await_batch(client, batch.id)

        by_key: dict[str, JsonResult] = {}
        for entry in client.messages.batches.results(batch.id):
            by_key[entry.custom_id] = self._from_batch_entry(entry)

        return [
            by_key.get(
                task.key,
                JsonResult(key=task.key, error="no result returned for this task"),
            )
            for task in tasks
        ]

    def _await_batch(self, client, batch_id: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            batch = client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                return
            if time.monotonic() >= deadline:
                raise LLMError(
                    f"batch {batch_id} still {batch.processing_status} after "
                    f"{self.timeout_seconds:.0f}s -- results stay retrievable, "
                    "re-run to collect them"
                )
            time.sleep(self.poll_seconds)

    def _from_batch_entry(self, entry) -> JsonResult:
        outcome = entry.result.type
        if outcome != "succeeded":
            # Expired and canceled are re-runnable; the key makes that idempotent.
            return JsonResult(key=entry.custom_id, error=f"batch result {outcome}")
        message = entry.result.message
        return _parse(
            _first_text(message.content), entry.custom_id, self.model, message.usage
        )
