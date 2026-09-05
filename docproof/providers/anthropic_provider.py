from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from .base import (BatchRequest, BatchStatus, NormalizedUsage, ProviderError,
                   ProviderResult)
from .catalog import lookup

log = logging.getLogger("docproof.providers.anthropic")

# Anthropic reports "ended" once every request in the batch has settled; the
# per-request outcome is then read off each result.
_ENDED = "ended"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, max_retries: int = 2,
                 prompt_caching: bool = True, effort: str | None = "low"):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError(
                "No Claude API key found. Add one on the Settings screen, or "
                "set ANTHROPIC_API_KEY before starting docproof.")
        self.client = anthropic.Anthropic(api_key=key, max_retries=max_retries)
        self.prompt_caching = prompt_caching
        self.effort = effort


    def _params(self, *, model: str, system: str, user: str,
                schema: dict[str, Any], max_tokens: int,
                cache: bool) -> dict[str, Any]:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": schema}}
        info = lookup(model)
        # effort is rejected outright on models that predate it (Haiku 4.5),
        # so it is catalog-gated rather than always-on.
        if self.effort and (info is None or info.supports_effort):
            output_config["effort"] = self.effort

        system_block: Any = system
        if cache:
            system_block = [{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}]
        return {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_block,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }


    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        # Stream and reassemble rather than a plain create(): the SDK refuses a
        # non-streaming request whose max_tokens could run past its 10-minute
        # ceiling, and the whole-book reads sit well over it — continuity alone
        # asks 32k, doubled to 64k on a truncation retry. Streaming changes
        # nothing else; get_final_message() returns the same Message create()
        # would have, so _to_result reads it unchanged. Batches are exempt (they
        # are asynchronous by construction) and stay non-streaming below.
        try:
            with self.client.messages.stream(
                **self._params(model=model, system=system, user=user,
                               schema=schema, max_tokens=max_tokens,
                               cache=self.prompt_caching)) as stream:
                resp = stream.get_final_message()
        except anthropic.APIStatusError as e:
            return ProviderResult(stop_reason="error",
                                  error=f"{e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            return ProviderResult(stop_reason="error", error=str(e))
        return _to_result(resp)


    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str:
        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=r.custom_id,
                    params=MessageCreateParamsNonStreaming(
                        **self._params(model=model, system=r.system,
                                       user=r.user, schema=r.schema,
                                       max_tokens=max_tokens,
                                       cache=self.prompt_caching)))
            for r in requests])
        log.info("Submitted Anthropic batch %s (%d requests)",
                 batch.id, len(requests))
        return batch.id

    def poll_batch(self, batch_id: str) -> BatchStatus:
        batch = self.client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        succeeded = counts.succeeded
        errored = counts.errored + counts.canceled + counts.expired
        total = succeeded + errored + counts.processing
        state = "completed" if batch.processing_status == _ENDED else "in_progress"
        return BatchStatus(state=state, total=total, succeeded=succeeded,
                           errored=errored)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        out: dict[str, ProviderResult] = {}
        for entry in self.client.messages.batches.results(batch_id):
            kind = entry.result.type
            if kind == "succeeded":
                out[entry.custom_id] = _to_result(entry.result.message)
            else:
                error = getattr(getattr(entry.result, "error", None),
                                "type", kind)
                out[entry.custom_id] = ProviderResult(
                    stop_reason="error", error=f"batch request {kind}: {error}")
        return out


def _to_result(message: Any) -> ProviderResult:
    usage = _usage(message.usage)
    if message.stop_reason == "refusal":
        return ProviderResult(usage=usage, stop_reason="refusal",
                              error="model declined this chunk")
    if message.stop_reason == "max_tokens":
        return ProviderResult(usage=usage, stop_reason="max_tokens",
                              error="output truncated")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"unparseable JSON: {e}")
    return ProviderResult(parsed=parsed, usage=usage)


def _usage(u: Any) -> NormalizedUsage:
    return NormalizedUsage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(
            u, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
    )
