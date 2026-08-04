from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .base import (BatchRequest, BatchStatus, NormalizedUsage, ProviderError,
                   ProviderResult, inlined_json_schema)
from .catalog import lookup

log = logging.getLogger("docproof.providers.gemini")

# Gemini's batch lifecycle, mapped onto the three outcomes the pipeline knows.
# PARTIALLY_SUCCEEDED is deliberately "completed": per-request failures are
# read off the individual responses, and the chunks that did come back are
# worth applying.
_STATES = {
    "JOB_STATE_SUCCEEDED": "completed",
    "JOB_STATE_PARTIALLY_SUCCEEDED": "completed",
    "JOB_STATE_FAILED": "failed",
    "JOB_STATE_EXPIRED": "expired",
    "JOB_STATE_CANCELLED": "cancelled",
    "JOB_STATE_CANCELLING": "in_progress",
}

# Reasoning depth. docproof speaks in efforts; Gemini takes a level.
_EFFORT_TO_LEVEL = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH",
                    "xhigh": "HIGH", "max": "HIGH"}

_CUSTOM_ID = "docproof_custom_id"


class GeminiProvider:
    name = "gemini"

    def __init__(self, *, api_key: str | None = None, max_retries: int = 2,
                 prompt_caching: bool = True, effort: str | None = "low"):
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ProviderError(
                "No Gemini API key found. Add one on the Settings screen, or "
                "set GEMINI_API_KEY before starting docproof.")
        self.client = genai.Client(api_key=key)
        # Gemini caches long prompt prefixes implicitly; there is no
        # per-request flag, so prompt_caching is accepted and ignored.
        self.max_retries = max_retries
        self.effort = effort

    # -- request construction -------------------------------------------------

    def _config(self, *, system: str, schema: dict[str, Any],
                max_tokens: int) -> types.GenerateContentConfig:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=inlined_json_schema(schema),
            max_output_tokens=max_tokens,
        )
        level = _EFFORT_TO_LEVEL.get(self.effort or "")
        if level:
            config.thinking_config = types.ThinkingConfig(thinking_level=level)
        return config

    # -- synchronous ----------------------------------------------------------

    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        try:
            resp = self.client.models.generate_content(
                model=model, contents=user,
                config=self._config(system=system, schema=schema,
                                    max_tokens=max_tokens))
        except genai_errors.APIError as e:
            return ProviderResult(stop_reason="error",
                                  error=f"{e.code}: {e.message}")
        return _to_result(resp)

    # -- batch ----------------------------------------------------------------

    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str:
        # custom_id rides in per-request metadata. Gemini returns inlined
        # responses in submission order, but order is a weaker promise than a
        # label, and collect() falls back to it only if metadata is absent.
        src = [
            types.InlinedRequest(
                model=model,
                contents=r.user,
                metadata={_CUSTOM_ID: r.custom_id},
                config=self._config(system=r.system, schema=r.schema,
                                    max_tokens=max_tokens))
            for r in requests]
        job = self.client.batches.create(model=model, src=src)
        log.info("Submitted Gemini batch %s (%d requests)", job.name,
                 len(requests))
        return job.name

    def poll_batch(self, batch_id: str) -> BatchStatus:
        job = self.client.batches.get(name=batch_id)
        state = _STATES.get(str(getattr(job.state, "name", job.state)),
                            "in_progress")
        stats = job.completion_stats
        succeeded = int(getattr(stats, "successful_count", 0) or 0)
        errored = int(getattr(stats, "failed_count", 0) or 0)
        total = succeeded + errored + int(
            getattr(stats, "incomplete_count", 0) or 0)
        return BatchStatus(state=state, total=total, succeeded=succeeded,
                           errored=errored)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        job = self.client.batches.get(name=batch_id)
        responses = list(getattr(job.dest, "inlined_responses", None) or [])
        # The submitted requests carry the ids; re-reading them here is what
        # makes collection work in a process that never saw the submission.
        submitted = list(getattr(job.src, "inlined_requests", None) or [])

        out: dict[str, ProviderResult] = {}
        for i, entry in enumerate(responses):
            custom_id = _custom_id(entry, submitted, i)
            if custom_id is None:
                log.warning("Batch response %d carries no request id and has "
                            "no matching request; skipping.", i)
                continue
            if entry.error is not None:
                out[custom_id] = ProviderResult(
                    stop_reason="error",
                    error=f"batch request failed: {entry.error}")
                continue
            out[custom_id] = _to_result(entry.response)
        return out


def _custom_id(entry: Any, submitted: list, index: int) -> str | None:
    for source in (entry, submitted[index] if index < len(submitted) else None):
        meta = getattr(source, "metadata", None) or {}
        if meta.get(_CUSTOM_ID):
            return str(meta[_CUSTOM_ID])
    return None


def _to_result(response: Any) -> ProviderResult:
    """Read one generate_content response. Shared by the synchronous path and
    the batch collector, so both surface identical stop reasons."""
    if response is None:
        return ProviderResult(stop_reason="error", error="empty response")
    usage = _usage(getattr(response, "usage_metadata", None))

    candidate = next(iter(getattr(response, "candidates", None) or []), None)
    finish = str(getattr(getattr(candidate, "finish_reason", None), "name", "")
                 or "")
    if finish == "SAFETY" or finish == "PROHIBITED_CONTENT":
        return ProviderResult(usage=usage, stop_reason="refusal",
                              error="model declined this chunk")
    if finish == "MAX_TOKENS":
        return ProviderResult(usage=usage, stop_reason="max_tokens",
                              error="output truncated")

    text = _output_text(candidate)
    if not text:
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"empty response ({finish or 'no reason'})")
    try:
        return ProviderResult(parsed=json.loads(text), usage=usage)
    except json.JSONDecodeError as e:
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"unparseable JSON: {e}")


def _output_text(candidate: Any) -> str:
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(p.text for p in parts if getattr(p, "text", None))


def _usage(u: Any) -> NormalizedUsage:
    if u is None:
        return NormalizedUsage()
    cached = int(getattr(u, "cached_content_token_count", 0) or 0)
    prompt = int(getattr(u, "prompt_token_count", 0) or 0)
    # Gemini counts cached tokens inside prompt_token_count and bills thinking
    # tokens as output; docproof reports cache reads separately and lumps
    # thinking in with the output it paid for.
    return NormalizedUsage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=int(getattr(u, "candidates_token_count", 0) or 0)
        + int(getattr(u, "thoughts_token_count", 0) or 0),
        cache_read_input_tokens=cached,
    )
