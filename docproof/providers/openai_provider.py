from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

import openai

from .base import (BatchRequest, BatchStatus, NormalizedUsage, ProviderError,
                   ProviderResult)
from .catalog import lookup

log = logging.getLogger("docproof.providers.openai")

_ENDPOINT = "/v1/responses"


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, api_key: str | None = None, max_retries: int = 2,
                 prompt_caching: bool = True, effort: str | None = "low"):
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError(
                "No ChatGPT API key found. Add one on the Settings screen, or "
                "set OPENAI_API_KEY before starting docproof.")
        self.client = openai.OpenAI(api_key=key, max_retries=max_retries)
        # OpenAI caches long prompt prefixes automatically — there is no
        # per-request flag to set, so prompt_caching is accepted and ignored.
        self.effort = effort

    def _body(self, *, model: str, system: str, user: str,
              schema: dict[str, Any], schema_name: str,
              max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
            "text": {"format": {"type": "json_schema", "name": schema_name,
                                "schema": schema, "strict": True}},
        }
        info = lookup(model)
        if self.effort and (info is None or info.supports_effort):
            body["reasoning"] = {"effort": self.effort}
        return body


    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        try:
            resp = self.client.responses.create(
                **self._body(model=model, system=system, user=user,
                             schema=schema, schema_name=schema_name,
                             max_tokens=max_tokens))
        except openai.APIStatusError as e:
            return ProviderResult(stop_reason="error",
                                  error=f"{e.status_code}: {e.message}")
        except openai.APIConnectionError as e:
            return ProviderResult(stop_reason="error", error=str(e))
        return result_from_response(resp.model_dump())


    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str:
        lines = [
            json.dumps({"custom_id": r.custom_id, "method": "POST",
                        "url": _ENDPOINT,
                        "body": self._body(model=model, system=r.system,
                                           user=r.user, schema=r.schema,
                                           schema_name=r.schema_name,
                                           max_tokens=max_tokens)},
                       ensure_ascii=False)
            for r in requests]
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        upload = self.client.files.create(
            file=("docproof-batch.jsonl", payload), purpose="batch")
        batch = self.client.batches.create(
            input_file_id=upload.id, endpoint=_ENDPOINT,
            completion_window="24h")
        log.info("Submitted OpenAI batch %s (%d requests)",
                 batch.id, len(requests))
        return batch.id

    def poll_batch(self, batch_id: str) -> BatchStatus:
        batch = self.client.batches.retrieve(batch_id)
        counts = batch.request_counts
        total = getattr(counts, "total", 0) or 0
        succeeded = getattr(counts, "completed", 0) or 0
        errored = getattr(counts, "failed", 0) or 0
        state = {
            "completed": "completed", "failed": "failed",
            "expired": "expired", "cancelled": "cancelled",
            "cancelling": "in_progress", "validating": "in_progress",
            "in_progress": "in_progress", "finalizing": "in_progress",
        }.get(batch.status, "in_progress")
        return BatchStatus(state=state, total=total, succeeded=succeeded,
                           errored=errored)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        batch = self.client.batches.retrieve(batch_id)
        out: dict[str, ProviderResult] = {}
        for file_id in (batch.output_file_id, batch.error_file_id):
            if not file_id:
                continue
            text = self.client.files.content(file_id).text
            for line in text.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                custom_id = entry.get("custom_id", "")
                if entry.get("error"):
                    out[custom_id] = ProviderResult(
                        stop_reason="error", error=str(entry["error"]))
                    continue
                response = entry.get("response") or {}
                if response.get("status_code") != 200:
                    out[custom_id] = ProviderResult(
                        stop_reason="error",
                        error=f"HTTP {response.get('status_code')}")
                    continue
                out[custom_id] = result_from_response(response.get("body") or {})
        return out


def result_from_response(body: dict[str, Any]) -> ProviderResult:
    """Read a Responses API payload. Shared by the synchronous path (via
    model_dump) and the batch path (raw JSON off the results file), so both
    surface identical stop reasons."""
    usage = _usage(body.get("usage") or {})

    refusal = _first_refusal(body)
    if refusal is not None:
        return ProviderResult(usage=usage, stop_reason="refusal", error=refusal)

    if body.get("status") == "incomplete":
        reason = (body.get("incomplete_details") or {}).get("reason", "")
        if reason == "max_output_tokens":
            return ProviderResult(usage=usage, stop_reason="max_tokens",
                                  error="output truncated")
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"incomplete: {reason or 'unknown'}")

    text = _output_text(body)
    if not text:
        return ProviderResult(usage=usage, stop_reason="error",
                              error="empty response")
    try:
        return ProviderResult(parsed=json.loads(text), usage=usage)
    except json.JSONDecodeError as e:
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"unparseable JSON: {e}")


def _first_refusal(body: dict[str, Any]) -> str | None:
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                return part.get("refusal") or "model declined this chunk"
    return None


def _output_text(body: dict[str, Any]) -> str:
    parts = [part.get("text", "")
             for item in body.get("output") or []
             if item.get("type") == "message"
             for part in item.get("content") or []
             if part.get("type") == "output_text"]
    return "".join(parts)


def _usage(u: dict[str, Any]) -> NormalizedUsage:
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    total_in = u.get("input_tokens", 0) or 0
    # OpenAI counts cached tokens inside input_tokens; docproof reports the two
    # separately, so subtract to keep the summary's arithmetic honest.
    return NormalizedUsage(
        input_tokens=max(total_in - cached, 0),
        output_tokens=u.get("output_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )
