"""The whitelist, and the JSON Schema it becomes.

Plan §5: "the whitelist *is* the schema". This module is where that stops
being a slogan. `whitelist.json` is read once and compiled into a schema with
`additionalProperties: false`, which means the API itself rejects a field the
human did not ask for. The model is not instructed not to invent a field; it
is structurally unable to return one.

Every property is nullable and every property is required. Those two together
are Stage 3's rule -- *extract only what is explicitly present, emit null
otherwise* -- in a form the transport enforces: the model must answer for each
field, and "not in the post" is a legal answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

WHITELIST_PATH = Path(__file__).with_name("whitelist.json")

TYPE_STRING = "string"
TYPE_STRING_LIST = "string_list"
TYPES = (TYPE_STRING, TYPE_STRING_LIST)

# Reported by the model, used only to route an item to the human. It never
# changes a value and never suppresses one: a field is extracted or it is null,
# and a low number means "look at this one", not "trust this less".
CONFIDENCE = "confidence"

INSTRUCTIONS = """\
You extract facts from a marketing post into a fixed set of fields. You are a \
transcriber, not an analyst.

Absolute rules:
1. Copy values from the post verbatim. Never rephrase, translate, normalise, \
convert, total or round anything.
2. If a field is not stated outright in the post, return null for it. Never \
infer, estimate or guess a value from context, and never carry a value over \
from one field to another.
3. Never summarise the post and never comment on it. Return only the fields.
4. A value you are not certain is actually present in the text is a null.

Fields to extract:
{fields}

Also return "{confidence}": a number from 0 to 1 for how plainly the values \
you did return were stated in the post. It is used to decide which posts a \
person re-reads, so report it honestly and never let it change what you \
extract."""


@dataclass(frozen=True)
class WhitelistField:
    name: str
    type: str
    description: str

    def as_property(self) -> dict[str, Any]:
        """Nullable by construction -- `anyOf` with null, not a bare type.

        Structured outputs support `anyOf` explicitly; a union spelled as a
        type array is not in the supported subset, and would be rejected or,
        worse, quietly ignored.
        """
        value: dict[str, Any] = (
            {"type": "string"}
            if self.type == TYPE_STRING
            else {"type": "array", "items": {"type": "string"}}
        )
        return {"anyOf": [value, {"type": "null"}], "description": self.description}


@dataclass(frozen=True)
class Whitelist:
    """The human's list of allowed facts, and nothing else."""

    version: str
    fields: tuple[WhitelistField, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    def schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            field.name: field.as_property() for field in self.fields
        }
        properties[CONFIDENCE] = {
            "type": "number",
            "description": "0..1, how plainly the returned values were stated.",
        }
        return {
            "type": "object",
            # The load-bearing line: the model cannot return a field the human
            # did not put in whitelist.json.
            "additionalProperties": False,
            "required": [*self.names, CONFIDENCE],
            "properties": properties,
        }

    def instructions(self) -> str:
        """The stable prefix of every extraction call."""
        listed = "\n".join(
            f"- {field.name}: {field.description}" for field in self.fields
        )
        return INSTRUCTIONS.format(fields=listed, confidence=CONFIDENCE)

    def values(self, data: dict[str, Any]) -> list[tuple[str, str]]:
        """Flatten a model response into (name, value) pairs worth storing.

        Nulls, empty strings and empty lists are dropped rather than stored:
        "this post does not say" is the absence of a fact, and the store holds
        facts. Anything outside the whitelist is ignored even if it arrives --
        the schema should prevent it, and this is the belt to that braces.
        """
        pairs: list[tuple[str, str]] = []
        for field in self.fields:
            value = data.get(field.name)
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            for item in items:
                text = str(item).strip()
                if text:
                    pairs.append((field.name, text))
        return pairs

    @staticmethod
    def confidence(data: dict[str, Any]) -> float | None:
        """Read the reported confidence, tolerating a model that omits it."""
        raw = data.get(CONFIDENCE)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        return min(1.0, max(0.0, float(raw)))


def load(path: Path | None = None) -> Whitelist:
    """Read and validate the whitelist.

    Validation is strict because this file is a schema in disguise: a typo in
    a type name would otherwise reach the API as a malformed request, and the
    error would be about JSON Schema rather than about the typo.
    """
    path = path or WHITELIST_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))

    version = str(raw.get("version") or "").strip()
    if not version:
        raise ValueError(f"{path.name} needs a version -- it is the prompt_version")

    entries = raw.get("fields")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path.name} needs a non-empty 'fields' list")

    fields: list[WhitelistField] = []
    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("type") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not name or not description:
            raise ValueError(f"{path.name}: every field needs a name and a description")
        if kind not in TYPES:
            raise ValueError(
                f"{path.name}: field {name!r} has type {kind!r}, "
                f"expected one of {', '.join(TYPES)}"
            )
        if name in seen:
            raise ValueError(f"{path.name}: duplicate field {name!r}")
        if name == CONFIDENCE:
            raise ValueError(f"{path.name}: {CONFIDENCE!r} is reserved")
        seen.add(name)
        fields.append(WhitelistField(name=name, type=kind, description=description))

    return Whitelist(version=version, fields=tuple(fields))


@lru_cache(maxsize=1)
def default() -> Whitelist:
    """The project's whitelist, parsed once.

    Cached because the instructions are the cacheable prefix of every call --
    rebuilding the string per post would be wasted work, and any variation in
    it would invalidate the prompt cache on every request.
    """
    return load()
