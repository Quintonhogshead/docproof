"""Provider contract: schema shape both vendors accept, usage normalization,
stop-reason handling, and the analyzer's behaviour against a fake."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from docproof.analyzer import Analyzer, build_output_model
from docproof.config import Config
from docproof.error_registry import load_error_types
from docproof.models import Chunk, ParagraphRef, Usage
from docproof.providers import (ProviderError, ProviderResult, build_provider,
                                estimate_cost, lookup, provider_for,
                                strict_json_schema)
from docproof.providers.openai_provider import result_from_response
from .fakes import USAGE, FakeProvider, finding_result, ids
from .test_error_types import ERROR_DIR


def _chunk(text: str = "The manuscript was finished, nobody wanted it.") -> Chunk:
    para = ParagraphRef(para_id="body-0000", part="word/document.xml",
                        location="body", text=text, style="Normal")
    return Chunk(chunk_id="chunk-000", paragraphs=(para,), est_tokens=20)


def _analyzer(provider, keys=("comma_splice",)) -> Analyzer:
    registry = load_error_types(ERROR_DIR, list(keys))
    return Analyzer(Config(), list(registry.values()), provider, ids())


# --- schema -------------------------------------------------------------------

def test_strict_schema_satisfies_both_vendors():
    schema = strict_json_schema(build_output_model(("spelling", "comma_splice")))
    finding = next(iter(schema["$defs"].values()))

    # Both vendors reject open objects; OpenAI also requires every property to
    # be listed as required, even ones with a Python-side default.
    for node in (schema, finding):
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])

    assert finding["properties"]["error_type"]["enum"] == ["spelling",
                                                           "comma_splice"]
    # Annotation-only keywords are stripped: neither vendor accepts them in
    # strict mode, and pydantic emits them for every defaulted field.
    assert "default" not in json.dumps(schema)


def test_dropping_explanations_removes_them_from_schema_and_prompt():
    from docproof.analyzer import build_system_prompt
    from docproof.error_registry import load_error_types

    types = list(load_error_types(ERROR_DIR, ["comma_splice"]).values())
    lean = strict_json_schema(build_output_model(("comma_splice",),
                                                 explanations=False))
    finding = next(iter(lean["$defs"].values()))
    # Asking for an explanation and discarding it would cost the same, so the
    # field has to leave the schema, not just the report.
    assert "explanation" not in finding["properties"]
    assert "explanation" not in finding["required"]
    assert "corrected_text" in finding["properties"]

    prompt = build_system_prompt(types, explanations=False)
    assert "explanation" not in prompt
    assert "explanation" in build_system_prompt(types, explanations=True)


def test_analyzer_without_explanations_still_produces_findings():
    cfg = Config(report_explanations=False)
    registry = load_error_types(ERROR_DIR, ["comma_splice"])
    provider = FakeProvider([ProviderResult(parsed={"findings": [{
        "para_id": "body-0000", "error_type": "comma_splice",
        "original_text": "The manuscript was finished, nobody wanted it.",
        "occurrence": 1,
        "corrected_text": "The manuscript was finished; nobody wanted it.",
        "confidence": "high"}]}, usage=USAGE)])
    findings = Analyzer(cfg, list(registry.values()), provider,
                        ids()).analyze_chunk(_chunk(), Usage())

    assert [f.error_type for f in findings] == ["comma_splice"]
    assert findings[0].explanation == ""      # nothing asked for, nothing lost


# --- provider selection -------------------------------------------------------

def test_catalog_drives_provider_selection():
    assert provider_for("claude-opus-5", "openai") == "anthropic"
    assert provider_for("gpt-5.6-terra", "anthropic") == "openai"
    assert provider_for("gemini-3.6-flash", "openai") == "gemini"
    # Unknown model falls back to the configured provider.
    assert provider_for("some-future-model", "openai") == "openai"


@pytest.mark.parametrize("model,var,name", [
    ("claude-opus-5", "ANTHROPIC_API_KEY", "Claude"),
    ("gpt-5.6-terra", "OPENAI_API_KEY", "ChatGPT"),
    ("gemini-3.6-flash", "GEMINI_API_KEY", "Gemini"),
])
def test_build_provider_reports_missing_key(monkeypatch, model, var, name):
    monkeypatch.delenv(var, raising=False)
    # The message is read in the app's Settings screen as well as the terminal,
    # so it leads with the product name, not the environment variable.
    with pytest.raises(ProviderError, match=f"No {name} API key"):
        build_provider(Config(api={"model": model}))


def test_anthropic_requests_omit_what_fable_rejects(monkeypatch):
    """Fable 5 always thinks and takes no sampling parameters: sending a
    `thinking` config, `temperature`, `top_p`, or `top_k` is a 400 on it.
    docproof sends none of them, and this pins that so a future edit that
    adds one fails here rather than at the vendor."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from docproof.providers.anthropic_provider import AnthropicProvider

    params = AnthropicProvider(effort="low")._params(
        model="claude-fable-5", system="s", user="u", schema={"type": "object"},
        max_tokens=16000, cache=True)

    assert not {"thinking", "temperature", "top_p", "top_k"} & set(params)
    # Depth is asked for the way that model accepts.
    assert params["output_config"]["effort"] == "low"


def test_cost_estimate_uses_catalog_and_batch_discount():
    full = estimate_cost("claude-opus-5", input_tokens=1_000_000,
                         output_tokens=1_000_000)
    assert full == pytest.approx(30.0)          # $5 in + $25 out
    half = estimate_cost("claude-opus-5", input_tokens=1_000_000,
                         output_tokens=1_000_000, batch=True)
    assert half == pytest.approx(15.0)
    # Fable is the priciest row in the catalog — twice Opus.
    assert estimate_cost("claude-fable-5", input_tokens=1_000_000,
                         output_tokens=1_000_000) == pytest.approx(60.0)
    assert estimate_cost("not-a-model", input_tokens=1, output_tokens=1) is None


def test_every_catalog_model_has_sane_pricing():
    from docproof.providers.catalog import MODELS
    assert {m.provider for m in MODELS} == {"anthropic", "openai", "gemini"}
    for m in MODELS:
        assert m.input_per_mtok > 0 and m.output_per_mtok > m.input_per_mtok
        assert lookup(m.id) is m


# --- analyzer against a fake --------------------------------------------------

def test_analyzer_passes_prompt_and_schema_to_provider():
    provider = FakeProvider()
    analyzer = _analyzer(provider)
    analyzer.analyze_chunk(_chunk(), Usage())

    call = provider.calls[0]
    assert call["model"] == Config().api.model
    assert "ERROR TYPE: comma_splice" in call["system"]
    assert "body-0000" in call["user"]
    assert call["schema"]["$defs"]


def test_analyzer_happy_path_and_usage_accounting():
    provider = FakeProvider([finding_result(
        para_id="body-0000", error_type="comma_splice",
        original="The manuscript was finished, nobody wanted it.",
        corrected="The manuscript was finished; nobody wanted it.")])
    usage = Usage()
    findings = _analyzer(provider).analyze_chunk(_chunk(), usage)

    assert [f.error_type for f in findings] == ["comma_splice"]
    assert usage.api_calls == 1
    assert usage.input_tokens == USAGE.input_tokens
    assert usage.cache_read_input_tokens == USAGE.cache_read_input_tokens


@pytest.mark.parametrize("result", [
    ProviderResult(stop_reason="refusal", error="declined"),
    ProviderResult(stop_reason="max_tokens", error="truncated"),
    ProviderResult(stop_reason="error", error="503"),
    ProviderResult(parsed={"findings": [{"para_id": "body-0000"}]}),  # malformed
])
def test_analyzer_drops_bad_results_without_raising(result):
    usage = Usage()
    assert _analyzer(FakeProvider([result])).analyze_chunk(_chunk(), usage) == []
    assert usage.api_calls == 1      # the attempt is still counted


# --- OpenAI response parsing (no network) -------------------------------------

def _openai_body(**over):
    body = {
        "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"findings": []}'}]}],
        "usage": {"input_tokens": 300, "output_tokens": 40,
                  "input_tokens_details": {"cached_tokens": 200}},
    }
    body.update(over)
    return body


def test_openai_usage_splits_cached_from_fresh_input():
    result = result_from_response(_openai_body())
    assert result.stop_reason == "ok"
    assert result.parsed == {"findings": []}
    # OpenAI counts cached tokens inside input_tokens; docproof reports them
    # separately, so the two must sum back to the vendor's total.
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 200


def test_openai_truncation_and_refusal_map_to_stop_reasons():
    truncated = result_from_response(_openai_body(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"}))
    assert truncated.stop_reason == "max_tokens"

    refused = result_from_response(_openai_body(output=[
        {"type": "message", "content": [{"type": "refusal",
                                         "refusal": "no thanks"}]}]))
    assert refused.stop_reason == "refusal"

    empty = result_from_response(_openai_body(output=[]))
    assert empty.stop_reason == "error"


# --- Gemini schema and response parsing (no network) --------------------------

def test_gemini_schema_is_self_contained():
    from docproof.providers import inlined_json_schema

    schema = inlined_json_schema(
        strict_json_schema(build_output_model(("spelling", "comma_splice"))))
    # Gemini reads one schema, not a document with a definitions section.
    text = json.dumps(schema)
    assert "$defs" not in text and "$ref" not in text
    finding = schema["properties"]["findings"]["items"]
    assert finding["properties"]["para_id"]["type"] == "string"
    assert finding["additionalProperties"] is False


def test_gemini_schema_widens_const_to_enum():
    from docproof.providers import inlined_json_schema

    # A one-key pass makes pydantic emit `const`, which Gemini does not take.
    schema = inlined_json_schema(
        strict_json_schema(build_output_model(("spelling",))))
    error_type = schema["properties"]["findings"]["items"]["properties"][
        "error_type"]
    assert error_type["enum"] == ["spelling"]
    assert "const" not in error_type


def _gemini_response(text='{"findings": []}', finish="STOP", usage=True):
    from google.genai import types

    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=text)]),
            finish_reason=getattr(types.FinishReason, finish))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=300, cached_content_token_count=200,
            candidates_token_count=30, thoughts_token_count=10)
        if usage else None)


def test_gemini_usage_splits_cache_and_counts_thinking_as_output():
    from docproof.providers.gemini_provider import _to_result

    result = _to_result(_gemini_response())
    assert result.stop_reason == "ok"
    assert result.parsed == {"findings": []}
    # prompt_token_count includes the cached tokens; docproof reports them apart.
    assert result.usage.input_tokens == 100
    assert result.usage.cache_read_input_tokens == 200
    # Thinking tokens bill as output, so they are counted as output.
    assert result.usage.output_tokens == 40


def test_gemini_truncation_and_refusal_map_to_stop_reasons():
    from docproof.providers.gemini_provider import _to_result

    assert _to_result(
        _gemini_response(finish="MAX_TOKENS")).stop_reason == "max_tokens"
    assert _to_result(
        _gemini_response(finish="SAFETY")).stop_reason == "refusal"
    assert _to_result(_gemini_response(text="")).stop_reason == "error"
    assert _to_result(_gemini_response(text="not json")).stop_reason == "error"
    assert _to_result(None).stop_reason == "error"


def test_gemini_batch_results_are_matched_by_request_id():
    """Order is how Gemini returns inlined responses, but the id is what the
    collector trusts — it runs in a process that never saw the submission."""
    from google.genai import types
    from docproof.providers.gemini_provider import _CUSTOM_ID, _custom_id

    responses = [types.InlinedResponse(metadata={_CUSTOM_ID: "p0-chunk-001"})]
    assert _custom_id(responses[0], [], 0) == "p0-chunk-001"

    # Metadata missing on the way back: fall back to the submitted request.
    submitted = [types.InlinedRequest(metadata={_CUSTOM_ID: "p1-chunk-002"})]
    assert _custom_id(types.InlinedResponse(), submitted, 0) == "p1-chunk-002"
    assert _custom_id(types.InlinedResponse(), [], 0) is None


def test_output_model_rejects_off_pass_error_type():
    model = build_output_model(("spelling",))
    with pytest.raises(ValidationError):
        model.model_validate({"findings": [{
            "para_id": "body-0000", "error_type": "comma_splice",
            "original_text": "a", "corrected_text": "b"}]})
