"""DeepInfra: hosted open-weight models behind an OpenAI-compatible endpoint.

Same weights a Mac Studio would run (gpt-oss, Qwen, DeepSeek), served per
token on DeepInfra's GPUs. No watermark is applied to output, inputs are not
retained after the response, and the licences are Apache 2.0 or MIT. The
manuscript does leave the building for the duration of the call — that is the
one thing this provider cannot promise that a local model can.

The wire format is OpenAI's *chat completions*, not the Responses API the
OpenAI provider speaks, so this is its own file rather than a base_url switch
on that one: the request body, the structured-output envelope, the usage
fields and the stop reasons all differ. DeepInfra has no batch endpoint;
`submit_batch` says so at submit time, before anything is billed.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Sequence

import openai

from .base import (BatchRequest, BatchStatus, NormalizedUsage, ProviderError,
                   ProviderResult, inlined_json_schema)
from .catalog import lookup

log = logging.getLogger("docproof.providers.deepinfra")

BASE_URL = "https://api.deepinfra.com/v1/openai"

_NO_BATCH = ("DeepInfra has no batch mode. Run this review now instead, or "
             "pick a Claude, ChatGPT, or Gemini model for a batch run.")

# Open reasoning models sometimes echo their thinking ahead of the answer even
# when the server was asked for JSON. Strip one leading think block; anything
# else that is not the object is a real error and is reported as such.
_THINK = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL)


class DeepInfraProvider:
    name = "deepinfra"

    def __init__(self, *, api_key: str | None = None, max_retries: int = 2,
                 prompt_caching: bool = True, effort: str | None = "low"):
        key = api_key or os.environ.get("DEEPINFRA_API_KEY")
        if not key:
            raise ProviderError(
                "No DeepInfra API key found. Add one on the Settings screen, "
                "or set DEEPINFRA_API_KEY before starting docproof.")
        self.client = openai.OpenAI(api_key=key, base_url=BASE_URL,
                                    max_retries=max_retries)
        # DeepInfra caches long prompt prefixes on the models that list a
        # cached-input rate; there is no per-request flag, so prompt_caching
        # is accepted and ignored, as the OpenAI provider does.
        self.effort = effort

    def _body(self, *, model: str, system: str, user: str,
              schema: dict[str, Any], schema_name: str,
              max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            # Self-contained, as for Gemini: the guided-decoding grammar these
            # servers compile from the schema is happier without $ref.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True,
                                "schema": inlined_json_schema(schema)}},
        }
        # Only the models the catalog marks as taking an effort get one. An
        # unknown model gets none: an unsupported parameter is a 400 here, not
        # a silently ignored field as it is at OpenAI.
        info = lookup(model)
        if self.effort and info is not None and info.supports_effort:
            body["reasoning_effort"] = _EFFORT.get(self.effort, "low")
        return body


    def complete_structured(self, *, model: str, system: str, user: str,
                            schema: dict[str, Any], schema_name: str,
                            max_tokens: int) -> ProviderResult:
        try:
            resp = self.client.chat.completions.create(
                **self._body(model=model, system=system, user=user,
                             schema=schema, schema_name=schema_name,
                             max_tokens=max_tokens))
        except openai.APIStatusError as e:
            return ProviderResult(stop_reason="error",
                                  error=f"{e.status_code}: {e.message}")
        except openai.APIConnectionError as e:
            return ProviderResult(stop_reason="error", error=str(e))
        return result_from_completion(resp.model_dump())


    def submit_batch(self, *, model: str, requests: Sequence[BatchRequest],
                     max_tokens: int) -> str:
        raise ProviderError(_NO_BATCH)

    def poll_batch(self, batch_id: str) -> BatchStatus:
        raise ProviderError(_NO_BATCH)

    def collect_batch(self, batch_id: str) -> dict[str, ProviderResult]:
        raise ProviderError(_NO_BATCH)


# The three levels chat completions accept; docproof's deeper two collapse onto
# high, as the Gemini provider does.
_EFFORT = {"low": "low", "medium": "medium", "high": "high",
           "xhigh": "high", "max": "high"}


def result_from_completion(body: dict[str, Any]) -> ProviderResult:
    """Read a chat-completions payload into the pipeline's result shape."""
    usage = _usage(body.get("usage") or {})
    choices = body.get("choices") or []
    if not choices:
        return ProviderResult(usage=usage, stop_reason="error",
                              error="empty response")
    choice = choices[0]
    message = choice.get("message") or {}

    refusal = message.get("refusal")
    if refusal:
        return ProviderResult(usage=usage, stop_reason="refusal", error=refusal)
    finish = choice.get("finish_reason")
    if finish == "content_filter":
        return ProviderResult(usage=usage, stop_reason="refusal",
                              error="model declined this chunk")
    if finish == "length":
        return ProviderResult(usage=usage, stop_reason="max_tokens",
                              error="output truncated")

    text = _THINK.sub("", message.get("content") or "", count=1)
    if not text.strip():
        return ProviderResult(usage=usage, stop_reason="error",
                              error="empty response")
    try:
        return ProviderResult(parsed=json.loads(text), usage=usage)
    except json.JSONDecodeError as e:
        return ProviderResult(usage=usage, stop_reason="error",
                              error=f"unparseable JSON: {e}")


def _usage(u: dict[str, Any]) -> NormalizedUsage:
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    total_in = u.get("prompt_tokens", 0) or 0
    # Cached tokens are counted inside prompt_tokens on the wire; docproof
    # reports the two separately, so subtract — the same split as OpenAI.
    return NormalizedUsage(
        input_tokens=max(total_in - cached, 0),
        output_tokens=u.get("completion_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )
