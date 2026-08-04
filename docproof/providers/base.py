"""The seam between the pipeline and whichever LLM vendor is answering.

Everything above this line (chunker, validator, reassembler) is provider-
agnostic already; everything below it is vendor SDK code. A provider speaks in
plain JSON objects, not pydantic models, so the synchronous path and the batch
path can share one implementation — batch results arrive as raw JSON with no
SDK object to unwrap.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel


class ProviderError(RuntimeError):
    """Configuration-level failure (missing key, unknown model). Raised at
    construction so it surfaces before a run starts, not mid-document."""


@dataclass
class NormalizedUsage:
    """Provider-neutral token counts. Field names deliberately match
    models.Usage so Usage.add() consumes this unchanged."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass(frozen=True)
class ProviderResult:
    """One completed request. `parsed` is the JSON object the model produced;
    the caller validates it against its own schema."""
    parsed: dict[str, Any] | None = None
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    stop_reason: str = "ok"          # ok | refusal | max_tokens | error
    error: str | None = None


@dataclass(frozen=True)
class BatchRequest:
    """One unit of work inside a batch. Each request carries its own prompt and
    schema, so every error-type pass over a document fits in a single batch
    instead of one batch per pass. custom_id round-trips through the provider
    untouched — results come back unordered and are matched by it."""
    custom_id: str
    system: str
    user: str
    schema: dict[str, Any]
    schema_name: str


@dataclass(frozen=True)
class BatchStatus:
    state: str                       # in_progress | completed | failed
                                     # | expired | cancelled
    total: int = 0
    succeeded: int = 0
    errored: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in ("completed", "failed", "expired", "cancelled")


@runtime_checkable
class Provider(Protocol):
    """What the pipeline needs from a vendor. Implementations live beside this
    file; nothing else in docproof imports a vendor SDK."""

    name: str

    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult: ...

    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str: ...

    def poll_batch(self, batch_id: str) -> BatchStatus: ...

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]: ...


# --- schema normalization -----------------------------------------------------

def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema both vendors accept in strict/structured-output mode.

    Pydantic's own schema is close but not sufficient: both providers require
    `additionalProperties: false` on every object, OpenAI additionally requires
    that every property appear in `required` (a field with a Python default is
    still a required key on the wire), and neither accepts stray annotation
    keywords. Normalizing here keeps the pydantic models readable — defaults
    stay where they belong, in the model."""
    return _normalize(copy.deepcopy(model.model_json_schema()))


def inlined_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The same schema with `$defs`/`$ref` resolved away and `const` widened
    to a one-value `enum`.

    Gemini's structured-output dialect is narrower than the one the other two
    vendors take: it reads a self-contained schema, not a document with a
    definitions section. Our schemas are shallow and non-recursive, so
    inlining is a substitution rather than a graph problem."""
    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, list):
            return [resolve(n, seen) for n in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            if name in seen:
                raise ValueError(
                    f"Schema definition {name!r} refers to itself; Gemini "
                    f"cannot express a recursive schema.")
            target = defs.get(name)
            if target is None:
                raise ValueError(f"Schema references missing definition {name!r}")
            merged = {**resolve(target, seen | {name}),
                      **{k: v for k, v in node.items() if k != "$ref"}}
            return merged
        out = {k: resolve(v, seen) for k, v in node.items() if k != "$defs"}
        if "const" in out:
            out["enum"] = [out.pop("const")]
        return out

    return resolve(schema)


_STRIP = ("default", "title", "minimum", "maximum", "exclusiveMinimum",
          "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
          "minItems", "maxItems", "pattern", "format")


def _normalize(node: Any) -> Any:
    if isinstance(node, list):
        return [_normalize(n) for n in node]
    if not isinstance(node, dict):
        return node

    out = {k: _normalize(v) for k, v in node.items() if k not in _STRIP}
    if "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out
