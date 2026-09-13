"""All providers and subscription transports are fake; no live model calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from docproof.config import Config
from docproof.providers import NormalizedUsage, ProviderError, ProviderResult
from docproof.resource_ledger import context_env, summarize, use_context
from galley import fixed_calls as fc


SCHEMA = {"type": "object", "properties": {"ready": {"type": "boolean"}},
          "required": ["ready"], "additionalProperties": False}
USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}
GOOD = ProviderResult(parsed={"ready": True}, usage=NormalizedUsage(input_tokens=100, output_tokens=10),
                      resource_usage=USAGE, actual_model="gpt-5.6-luna")


class FakeProvider:
    def __init__(self, answers=None):
        self.answers = list(answers or [GOOD])
        self.requests = []
        self.configs = []

    def factory(self, cfg, *, model):
        self.configs.append((cfg, model))
        return self

    def complete_structured(self, **kwargs):
        self.requests.append(kwargs)
        result = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(result, BaseException):
            raise result
        return result


def calls(tmp_path, provider=None, **kwargs):
    provider = provider or FakeProvider()
    return fc.FixedCalls(tmp_path / "fixed", {"source_sha256": "source", "recipe": "recipe-v1"},
                         Config(), provider_factory=provider.factory, **kwargs)


def ask(caller, **kwargs):
    return caller.ask("test-read", **{ "model": "gpt-5.6-luna", "system": "Only proofread the supplied evidence.",
        "user": "The manuscript excerpt.", "schema": SCHEMA, "schema_name": "test", "max_tokens": 100,
        **kwargs})


def test_completed_request_is_reused_across_restart_and_not_double_billed(tmp_path):
    provider = FakeProvider()
    ledger = tmp_path / "usage.jsonl"
    with use_context(context_env(ledger, "source", "recipe")):
        caller = calls(tmp_path, provider)
        assert ask(caller) == {"ready": True}
        restarted = calls(tmp_path, provider)
        assert ask(restarted) == {"ready": True}
        restarted.assert_complete()
    assert len(provider.requests) == 1
    assert caller.usage_summary()["calls"] == 1
    assert caller.usage_summary()["output_tokens"] == 10
    usage = summarize(ledger)
    assert usage["new_attempts"] == 1 and usage["reused_receipts"] == 1
    assert usage["output_tokens"] == 10


@pytest.mark.parametrize("changed", [{"user": "Changed manuscript"}, {"system": "Changed task"},
    {"effort": "medium"}, {"schema_name": "changed_schema"}, {"max_tokens": 101},
    {"schema": {**SCHEMA, "description": "Changed output contract"}}])
def test_changed_evidence_creates_distinct_call(tmp_path, changed):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    ask(caller)
    ask(caller, **changed)
    assert len(provider.requests) == 2


def test_changed_source_identity_is_never_reused(tmp_path):
    provider = FakeProvider()
    ask(calls(tmp_path, provider))
    changed = fc.FixedCalls(tmp_path / "fixed", {"source_sha256": "different", "recipe": "recipe-v1"},
                            Config(), provider_factory=provider.factory)
    ask(changed)
    assert len(provider.requests) == 2


def test_same_request_threads_submit_once(tmp_path):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ask(caller), range(4)))
    assert results == [{"ready": True}] * 4
    assert len(provider.requests) == 1


def test_interrupted_call_requires_reconciliation_even_after_restart(tmp_path):
    provider = FakeProvider([KeyboardInterrupt()])
    caller = calls(tmp_path, provider)
    with pytest.raises(KeyboardInterrupt):
        ask(caller)
    with pytest.raises(fc.FixedCallInterrupted, match="reconcile"):
        ask(calls(tmp_path, provider))
    with pytest.raises(fc.FixedCallError, match="before delivery"):
        caller.assert_complete()
    assert len(provider.requests) == 1
    assert caller.usage_summary()["charged_output_tokens"] == 100
    assert caller.usage_summary()["unknown_output_attempts"] == 1


def test_atomic_response_is_adopted_after_receipt_write_interruption(tmp_path, monkeypatch):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    original = fc._atomic

    def crash(path, value):
        if path.name == "receipt.json" and value.get("status") == "completed":
            raise KeyboardInterrupt()
        original(path, value)

    monkeypatch.setattr(fc, "_atomic", crash)
    with pytest.raises(KeyboardInterrupt):
        ask(caller)
    monkeypatch.setattr(fc, "_atomic", original)
    assert ask(calls(tmp_path, provider)) == {"ready": True}
    assert len(provider.requests) == 1
    caller.assert_complete()


@pytest.mark.parametrize("reason", ["max_tokens", "refusal"])
def test_complete_looking_truncated_or_refused_payload_never_passes(tmp_path, reason):
    provider = FakeProvider([replace(GOOD, stop_reason=reason)])
    caller = calls(tmp_path, provider)
    with pytest.raises(fc.FixedCallError, match=reason):
        ask(caller)
    with pytest.raises(fc.FixedCallError):
        ask(calls(tmp_path, provider))
    assert len(provider.requests) == 1


@pytest.mark.parametrize("parsed", [{}, {"ready": 1}, {"ready": True, "extra": 2}, []])
def test_schema_failures_are_bounded_and_persisted(tmp_path, parsed):
    provider = FakeProvider([replace(GOOD, parsed=parsed)])
    caller = calls(tmp_path, provider, max_attempts=2)
    with pytest.raises(fc.FixedCallError, match="exhausted"):
        ask(caller)
    with pytest.raises(fc.FixedCallError, match="exhausted"):
        ask(calls(tmp_path, provider))
    assert len(provider.requests) == 2
    assert caller.usage_summary()["calls"] == 2
    assert caller.usage_summary()["output_tokens"] == 20


def test_claude_named_wrapper_is_validated_reused_and_preserved(tmp_path):
    wrapped = {"test": {"ready": True}}
    provider = FakeProvider([replace(GOOD, parsed=wrapped)])
    caller = calls(tmp_path, provider)
    assert ask(caller, model="claude-sonnet-5") == {"ready": True}
    envelope = next(caller.directory.glob("calls/*/attempts/1/response.json"))
    saved = envelope.read_bytes()
    assert json.loads(saved)["result"]["parsed"] == wrapped
    assert ask(calls(tmp_path, provider), model="claude-sonnet-5") == {"ready": True}
    assert len(provider.requests) == 1 and envelope.read_bytes() == saved
    caller.assert_complete()


def test_old_wrapped_schema_failure_recovers_without_retry_or_rewriting_response(tmp_path, monkeypatch):
    provider = FakeProvider([replace(GOOD, parsed={"test": {"ready": True}})])
    normalize = fc._response_payload

    def old_validator(parsed, request):
        fc._schema(parsed, request["schema"])
        return parsed

    monkeypatch.setattr(fc, "_response_payload", old_validator)
    caller = calls(tmp_path, provider)
    with pytest.raises(fc.FixedCallError, match="exhausted"):
        ask(caller, model="claude-sonnet-5")
    saved = {p: p.read_bytes() for p in caller.directory.glob("calls/*/attempts/*/response.json")}
    limits = caller.usage_summary()["limits"]
    monkeypatch.setattr(fc, "_response_payload", normalize)
    resumed = calls(tmp_path, provider)
    assert ask(resumed, model="claude-sonnet-5") == {"ready": True}
    assert len(provider.requests) == 3
    assert resumed.usage_summary()["calls"] == 3
    assert resumed.usage_summary()["output_tokens"] == 30
    assert resumed.usage_summary()["limits"] == limits
    assert all(p.read_bytes() == contents for p, contents in saved.items())
    resumed.assert_complete()


@pytest.mark.parametrize("parsed", [
    {"test": {"ready": True}, "extra": "do not discard"},
    {"wrong_name": {"ready": True}},
    {"test": {"ready": "true"}},
    {"test": {"ready": True, "extra": 1}},
])
def test_claude_wrapper_never_relaxes_schema_or_discards_siblings(tmp_path, parsed):
    provider = FakeProvider([replace(GOOD, parsed=parsed)])
    caller = calls(tmp_path, provider, max_attempts=1)
    with pytest.raises(fc.FixedCallError, match="invalid_schema"):
        ask(caller, model="claude-sonnet-5")
    with pytest.raises(fc.FixedCallError, match="invalid_schema"):
        ask(calls(tmp_path, provider), model="claude-sonnet-5")
    assert len(provider.requests) == 1
    with pytest.raises(fc.FixedCallError):
        caller.assert_complete()


def test_api_output_is_not_unwrapped(tmp_path):
    provider = FakeProvider([replace(GOOD, parsed={"test": {"ready": True}})])
    with pytest.raises(fc.FixedCallError, match="invalid_schema"):
        ask(calls(tmp_path, provider, max_attempts=1))


def test_legitimate_root_field_named_after_schema_is_not_unwrapped(tmp_path):
    schema = {"type": "object", "properties": {"test": SCHEMA},
              "required": ["test"], "additionalProperties": False}
    wrapped = {"test": {"ready": True}}
    caller = calls(tmp_path, FakeProvider([replace(GOOD, parsed=wrapped)]))
    assert ask(caller, model="claude-sonnet-5", schema=schema) == wrapped
    caller.assert_complete()


def test_confirmed_failure_may_retry_but_network_ambiguity_may_not(tmp_path):
    provider = FakeProvider([ProviderResult(stop_reason="error", error="429: rate limited"), GOOD])
    caller = calls(tmp_path, provider)
    assert ask(caller) == {"ready": True}
    assert len(provider.requests) == 2
    assert caller.usage_summary()["unknown_output_attempts"] == 1
    caller.assert_complete()

    ambiguous = FakeProvider([ProviderResult(stop_reason="error", error="Connection dropped")])
    caller2 = calls(tmp_path / "other", ambiguous)
    with pytest.raises(fc.FixedCallInterrupted):
        ask(caller2)
    assert len(ambiguous.requests) == 1


def test_submitted_exception_preserves_unknown_usage_and_never_replays(tmp_path):
    provider = FakeProvider([TimeoutError("unknown outcome")])
    caller = calls(tmp_path, provider)
    with pytest.raises(fc.FixedCallInterrupted):
        ask(caller)
    with pytest.raises(fc.FixedCallInterrupted):
        ask(calls(tmp_path, provider))
    assert caller.usage_summary()["unknown_api_attempts"] == 1
    assert caller.usage_summary()["charged_api_usd"] > 0
    assert caller.usage_summary()["known_api_usd"] == 0


@pytest.mark.parametrize("limits", [{"max_calls": 1}, {"max_output_tokens": 109}])
def test_budget_is_persisted_and_cannot_be_raised_by_restart(tmp_path, limits):
    provider = FakeProvider()
    caller = calls(tmp_path, provider, **limits)
    ask(caller)
    restarted = calls(tmp_path, provider)
    with pytest.raises(fc.FixedCallBudgetExceeded):
        ask(restarted, user="Second read")
    assert len(provider.requests) == 1
    assert restarted.usage_summary()["limits"] == caller.usage_summary()["limits"]


def test_api_budget_reserves_before_spending_and_subscriptions_cost_zero(tmp_path):
    provider = FakeProvider()
    caller = calls(tmp_path, provider, max_api_usd=0)
    with pytest.raises(fc.FixedCallBudgetExceeded):
        ask(caller)
    assert provider.requests == []
    assert ask(caller, model="claude-sonnet-4-6") == {"ready": True}
    assert caller.usage_summary()["charged_api_usd"] == 0


def test_known_api_usage_releases_the_unused_reservation(tmp_path):
    caller = calls(tmp_path)
    ask(caller)
    summary = caller.usage_summary()
    assert summary["unknown_api_attempts"] == 0
    assert summary["charged_api_usd"] == summary["known_api_usd"] > 0


def test_routes_luna_api_claude_subscription_sol_and_astra_codex(tmp_path):
    provider = FakeProvider()
    subscription = []

    def codex(prompt, schema, work_dir, **kwargs):
        subscription.append((prompt, schema, work_dir, kwargs))
        return {"ready": True}

    caller = calls(tmp_path, provider, codex_runner=codex)
    ask(caller)
    ask(caller, model="claude-opus-5")
    ask(caller, model="gpt-5.6-sol", effort="high")
    ask(caller, model="gpt-6-astra", effort="high")
    assert [m for _, m in provider.configs] == ["gpt-5.6-luna", "claude-opus-5"]
    assert [item[3]["model"] for item in subscription] == ["gpt-5.6-sol", "gpt-6-astra"]
    assert all(item[3]["no_tools"] is True for item in subscription)
    assert all(cfg.api.claude_lane == "subagent" and cfg.api.max_retries == 0 for cfg, _ in provider.configs)
    assert all(cfg.api.effort == "low" for cfg, _ in provider.configs)


@pytest.mark.parametrize("model,transport", [("gpt-5.6-sol", "api"), ("gpt-6-astra", "api"),
    ("claude-opus-5", "api"), ("gpt-5.6-luna", "codex"), ("unknown", None)])
def test_forbidden_transport_never_falls_back(tmp_path, model, transport):
    provider = FakeProvider()
    with pytest.raises(fc.FixedCallError, match="no fallback"):
        ask(calls(tmp_path, provider), model=model, transport=transport)
    assert not provider.requests


def test_unavailable_provider_is_preflight_failure_without_spending(tmp_path):
    def unavailable(*args, **kwargs):
        raise ProviderError("subscription unavailable")

    caller = fc.FixedCalls(tmp_path, {}, Config(), provider_factory=unavailable)
    with pytest.raises(fc.FixedCallError, match="before submission"):
        ask(caller, model="claude-opus-5")
    assert caller.usage_summary()["calls"] == 0
    with pytest.raises(fc.FixedCallError, match="preflight_failed"):
        caller.assert_complete()


def test_provider_adapter_counts_only_new_usage(tmp_path):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    adapter = caller.provider("detector", Config())
    request = dict(model="gpt-5.6-luna", system="Proofread.", user="Text.",
                   schema=SCHEMA, schema_name="typed", max_tokens=100)
    first, cached = adapter.complete_structured(**request), adapter.complete_structured(**request)
    assert first.usage.output_tokens == 10
    assert cached.usage.output_tokens == 0 and cached.usage.billed is False
    assert cached.resource_usage is None
    assert len(provider.requests) == 1


def test_tampered_saved_response_is_never_used(tmp_path):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    ask(caller)
    path = next((tmp_path / "fixed/calls").glob("*/attempts/1/response.json"))
    envelope = json.loads(path.read_text())
    envelope["result"]["parsed"]["ready"] = False
    path.write_text(json.dumps(envelope))
    with pytest.raises(fc.FixedCallError, match="has changed"):
        ask(caller)
    assert len(provider.requests) == 1


def test_provider_default_zero_usage_remains_unknown(tmp_path):
    caller = calls(tmp_path, FakeProvider([ProviderResult(parsed={"ready": True})]))
    ask(caller)
    summary = caller.usage_summary()
    assert summary["unknown_output_attempts"] == 1
    assert summary["charged_output_tokens"] == 100


def test_schema_refs_and_nullable_fields_are_checked():
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/item"}}},
        "required": ["items"], "additionalProperties": False,
        "$defs": {"item": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    fc._schema({"items": [None, "one"]}, schema)
    with pytest.raises(fc.FixedCallError):
        fc._schema({"items": [1]}, schema)


def test_delivery_evidence_manifest_is_read_only_and_source_bound(tmp_path):
    provider = FakeProvider()
    caller = calls(tmp_path, provider)
    ask(caller)
    first = fc.validate_fixed_call_evidence(caller.directory, identity=caller.identity)
    second = fc.validate_fixed_call_evidence(caller.directory, identity=caller.identity)
    assert first == second and len(first) >= 4
    assert len(provider.requests) == 1
    assert {item["path"].split("/")[-1] for item in first} >= {"budget.json", "request.json", "receipt.json", "response.json"}
    with pytest.raises(fc.FixedCallError, match="different source"):
        fc.validate_fixed_call_evidence(caller.directory, identity={"source": "changed"})


def test_deleted_call_receipt_cannot_disappear_from_delivery_inventory(tmp_path):
    import shutil
    caller = calls(tmp_path)
    ask(caller)
    shutil.rmtree(next((caller.directory / "calls").iterdir()))
    with pytest.raises(fc.FixedCallError, match="inventory"):
        caller.assert_complete()


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "tool_use"])
def test_claude_terminal_stop_reason_overrides_plausible_json(tmp_path, monkeypatch, stop):
    import asyncio
    from docproof.providers import subagent

    async def terminal(self, *args, evidence, **kwargs):
        evidence["result"] = SimpleNamespace(stop_reason=stop, subtype="success", is_error=False, usage=USAGE)
        return GOOD

    monkeypatch.setattr(subagent, "availability", lambda: (True, "available"))
    monkeypatch.setattr(subagent.SubagentProvider, "_turn", terminal)
    provider = fc._default_provider(Config(), model="claude-opus-5")
    result = asyncio.run(provider._turn(evidence={}))
    assert result.stop_reason != "ok"


def fake_codex_transport(tmp_path, monkeypatch, *, fail_first=False):
    from galley import codex_runner as cr
    from pathlib import Path
    executions = []
    monkeypatch.setenv("GALLEY_CODEX_HOME", str(tmp_path / "subscription-login"))
    monkeypatch.setattr(cr, "_binary", lambda explicit: "fake-codex")
    monkeypatch.setattr(cr, "_check_login_locked", lambda *args, **kwargs: None)

    def execute(argv, **kwargs):
        executions.append(argv)
        failure = fail_first and len(executions) == 1
        if not failure:
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({"ready": True}))
        return {"returncode": 2 if failure else 0, "timed_out": False, "stdout_tail": "", "stderr_tail": "",
            "events": {"event_counts": {} if failure else {"turn.completed": 1},
                       "usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 10}}}

    monkeypatch.setattr(cr, "_execute", execute)
    return executions


def test_subscription_completion_survives_outer_receipt_crash(tmp_path, monkeypatch):
    executions = fake_codex_transport(tmp_path, monkeypatch)
    caller = calls(tmp_path)
    original = fc._atomic

    def crash(path, value):
        if path.name == "response.json":
            raise KeyboardInterrupt()
        original(path, value)

    monkeypatch.setattr(fc, "_atomic", crash)
    with pytest.raises(KeyboardInterrupt):
        ask(caller, model="gpt-5.6-sol")
    monkeypatch.setattr(fc, "_atomic", original)
    assert ask(calls(tmp_path), model="gpt-5.6-sol") == {"ready": True}
    assert len(executions) == 1
    assert caller.usage_summary()["input_tokens"] == 15
    assert caller.usage_summary()["output_tokens"] == 10
    caller.assert_complete()


def test_confirmed_subscription_failure_uses_persisted_retry_envelope(tmp_path, monkeypatch):
    executions = fake_codex_transport(tmp_path, monkeypatch, fail_first=True)
    caller = calls(tmp_path)
    assert ask(caller, model="gpt-5.6-sol") == {"ready": True}
    assert len(executions) == 2
    assert caller.usage_summary()["calls"] == 2
    assert caller.usage_summary()["output_tokens"] == 20
    caller.assert_complete()
    assert ask(calls(tmp_path), model="gpt-5.6-sol") == {"ready": True}
    assert len(executions) == 2


@pytest.mark.parametrize("kind", ["quota", "credentials"])
def test_worker_pause_exceptions_survive_and_only_retry_on_resume(tmp_path, kind):
    from docproof.subscription_limits import UsageLimitError
    from galley.driver import CredentialsError
    error = UsageLimitError("You've hit your usage limit") if kind == "quota" else CredentialsError("Not logged in")
    provider = FakeProvider([error, GOOD])
    caller = calls(tmp_path, provider)
    with pytest.raises(type(error)) as raised:
        ask(caller, model="claude-opus-5")
    assert raised.value is error
    assert len(provider.requests) == 1
    assert fc.fixed_usage_summary(caller.directory)["unknown_output_attempts"] == 1
    assert ask(calls(tmp_path, provider), model="claude-opus-5") == {"ready": True}
    assert len(provider.requests) == 2
    caller.assert_complete()


def test_preflight_credentials_exception_survives_without_a_submission(tmp_path):
    from galley.driver import CredentialsError
    error = CredentialsError("Not logged in")

    def unavailable(*args, **kwargs):
        raise error

    caller = fc.FixedCalls(tmp_path, {}, Config(), provider_factory=unavailable)
    with pytest.raises(CredentialsError) as raised:
        ask(caller, model="claude-opus-5")
    assert raised.value is error
    assert fc.fixed_usage_summary(caller.directory)["calls"] == 0


COVERAGE_SCHEMA = {"type": "object", "properties": {
    "reviewed_paragraph_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["reviewed_paragraph_ids"], "additionalProperties": False}
COVERAGE = {"reviewed_paragraph_ids": {"ids": ["p1", "p2"], "id_key": None, "context_ids": ["c1"]}}


def covered(caller, *, coverage=COVERAGE, model="claude-sonnet-5"):
    return caller.ask("typed", model=model, system="Review every assigned paragraph.",
        user='Source text containing literal <paragraph id="not-an-assignment"> markup.',
        schema=COVERAGE_SCHEMA, schema_name="findings", max_tokens=100, coverage=coverage)


@pytest.mark.parametrize("ids", [["p1"], ["p1", "p2", "unknown"], ["p1", "p1", "p2"]])
def test_incomplete_coverage_retries_only_failed_read_and_keeps_raw_evidence(tmp_path, ids):
    bad = replace(GOOD, parsed={"reviewed_paragraph_ids": ids})
    good = replace(GOOD, parsed={"reviewed_paragraph_ids": ["c1", "p2", "p1"]})
    provider = FakeProvider([bad, good])
    caller = calls(tmp_path, provider)
    assert covered(caller) == {"reviewed_paragraph_ids": ["p2", "p1"]}
    assert len(provider.requests) == 2
    assert caller.usage_summary()["calls"] == 2 and caller.usage_summary()["output_tokens"] == 20
    raw = {p: p.read_bytes() for p in caller.directory.glob("calls/*/attempts/*/response.json")}
    assert any(json.loads(value)["result"]["parsed"]["reviewed_paragraph_ids"] == ids for value in raw.values())
    assert covered(calls(tmp_path, provider)) == {"reviewed_paragraph_ids": ["p2", "p1"]}
    assert len(provider.requests) == 2 and all(p.read_bytes() == value for p, value in raw.items())
    caller.assert_complete()


def test_old_schema_valid_incomplete_cache_is_reconciled_with_same_request_and_budget(tmp_path):
    provider = FakeProvider([replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1"]})])
    caller = calls(tmp_path, provider)
    assert covered(caller, coverage=None) == {"reviewed_paragraph_ids": ["p1"]}
    folder = next((caller.directory / "calls").iterdir())
    original_request = (folder / "request.json").read_bytes()
    response = folder / "attempts/1/response.json"
    original_response = response.read_bytes()
    limits = caller.usage_summary()["limits"]
    provider.answers = [replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1", "p2"]})]
    assert covered(calls(tmp_path, provider)) == {"reviewed_paragraph_ids": ["p1", "p2"]}
    assert len(provider.requests) == 2 and len(list((caller.directory / "calls").iterdir())) == 1
    assert (folder / "request.json").read_bytes() == original_request
    assert response.read_bytes() == original_response
    assert caller.usage_summary()["limits"] == limits
    budget = json.loads((caller.directory / "budget.json").read_text())
    assert sorted(row["status"] for row in budget["entries"].values()) == ["completed", "failed"]
    caller.assert_complete()


def test_coverage_failure_exhaustion_never_grants_new_retry_allowance(tmp_path):
    provider = FakeProvider([replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1"]})])
    caller = calls(tmp_path, provider, max_attempts=2)
    for runner in (caller, calls(tmp_path, provider)):
        with pytest.raises(fc.FixedCallError, match="invalid_coverage.*exhausted"):
            covered(runner)
    assert len(provider.requests) == 2 and caller.usage_summary()["calls"] == 2
    with pytest.raises(fc.FixedCallError):
        caller.assert_complete()


@pytest.mark.parametrize("change", ["remove", "alter", "replace_inventory"])
def test_saved_coverage_cannot_be_deleted_or_weakened_to_reuse_answer(tmp_path, change):
    provider = FakeProvider([replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1", "p2"]})])
    caller = calls(tmp_path, provider)
    covered(caller)
    path = next(caller.directory.glob("calls/*/coverage.json"))
    coverage = None
    if change == "remove":
        path.unlink()
    elif change == "alter":
        path.write_bytes(path.read_bytes() + b" ")
    else:
        coverage = {"reviewed_paragraph_ids": {"ids": ["p1"], "id_key": None, "context_ids": ["p2"]}}
    with pytest.raises(fc.FixedCallContractError):
        covered(caller, coverage=coverage)
    assert len(provider.requests) == 1
    if change != "replace_inventory":
        with pytest.raises(fc.FixedCallContractError):
            caller.assert_complete()


def test_codex_incomplete_answer_uses_distinct_retry_receipt_then_caches_success(tmp_path):
    request_ids = []
    def codex(prompt, schema, directory, **kwargs):
        request_ids.append(kwargs["request_id"])
        return {"reviewed_paragraph_ids": ["p1"] if len(request_ids) == 1 else ["p1", "p2"]}
    caller = calls(tmp_path, codex_runner=codex)
    assert covered(caller, model="gpt-6-astra") == {"reviewed_paragraph_ids": ["p1", "p2"]}
    assert len(request_ids) == len(set(request_ids)) == 2
    assert request_ids[1] == request_ids[0] + ":attempt:2"
    assert covered(caller, model="gpt-6-astra") == {"reviewed_paragraph_ids": ["p1", "p2"]}
    assert len(request_ids) == 2
    caller.assert_complete()


@pytest.mark.parametrize("field,key", [("reviewed_ids", None), ("reviewed_check_ids", None),
    ("decisions", "id"), ("comment_decisions", "id"), ("paragraphs", "id")])
def test_each_inventory_contract_retries_missing_required_items(tmp_path, field, key):
    items = {"type": "string"} if key is None else {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}
    schema = {"type": "object", "properties": {field: {"type": "array", "items": items}}, "required": [field], "additionalProperties": False}
    rows = ["p1", "p2"] if key is None else [{"id": "p1"}, {"id": "p2"}]
    provider = FakeProvider([replace(GOOD, parsed={field: rows[:1]}), replace(GOOD, parsed={field: rows})])
    caller = calls(tmp_path, provider)
    result = caller.ask("review", model="gpt-5.6-luna", system="Check every assigned item.", user="Book context.",
        schema=schema, schema_name="review", max_tokens=100,
        coverage={field: {"ids": ["p1", "p2"], "id_key": key, "context_ids": []}})
    assert result == {field: rows} and len(provider.requests) == 2
    caller.assert_complete()


def test_old_incomplete_answer_cannot_expand_its_saved_single_attempt_allowance(tmp_path):
    provider = FakeProvider([replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1"]})])
    caller = calls(tmp_path, provider, max_attempts=1)
    covered(caller, coverage=None)
    resumed = calls(tmp_path, provider, max_attempts=3)
    with pytest.raises(fc.FixedCallError, match="invalid_coverage.*exhausted"):
        covered(resumed)
    assert len(provider.requests) == 1 and resumed.usage_summary()["calls"] == 1


def test_coverage_response_accounting_interruption_recovers_without_resubmitting(tmp_path, monkeypatch):
    provider = FakeProvider([replace(GOOD, parsed={"reviewed_paragraph_ids": ["p1", "p2"]})])
    caller = calls(tmp_path, provider)
    account = caller._account
    monkeypatch.setattr(caller, "_account", lambda *a, **k: (_ for _ in ()).throw(OSError("Interrupted accounting")))
    with pytest.raises(OSError, match="Interrupted accounting"):
        covered(caller)
    assert len(provider.requests) == 1
    monkeypatch.setattr(caller, "_account", account)
    assert covered(caller) == {"reviewed_paragraph_ids": ["p1", "p2"]}
    assert len(provider.requests) == 1
    caller.assert_complete()


def dispute_fixture():
    from galley.fixed_workflow import DECISIONS
    proposals = [
        {"id": "f-hyphen", "para_id": "p", "start": 4, "end": 5, "before": "-", "replacement": "", "action": "edit", "format": ""},
        {"id": "f-word", "para_id": "p", "start": 0, "end": 9, "before": "fami-liar", "replacement": "fame-liar", "action": "edit", "format": ""}]
    site = {"id": "d-site", "para_id": "p", "paragraph": "fami-liar face", "start": 0, "end": 9,
            "before": "fami-liar", "proposals": proposals}
    decisions = [{"id": p["id"], "action": "apply" if i == 0 else "drop", "replacement": "",
                  "reason": "Exact correction." if i == 0 else "False positive.",
                  "question": "", "missing_knowledge": ""} for i, p in enumerate(proposals)]
    kwargs = dict(model="claude-opus-5", system="Settle every grouped site.",
                  user=json.dumps({"sites": [site]}), schema=DECISIONS, schema_name="disputes",
                  coverage={"decisions": {"ids": ["d-site"], "id_key": "id", "context_ids": []}})
    return site, decisions, kwargs


def test_complete_nested_proposal_decisions_are_composed_from_exact_spans_and_reused(tmp_path):
    site, decisions, kwargs = dispute_fixture()
    provider = FakeProvider([replace(GOOD, parsed={"decisions": decisions})])
    caller = calls(tmp_path, provider)
    result = caller.ask("typed_disputes", **kwargs)
    assert result["decisions"][0]["id"] == "d-site"
    assert result["decisions"][0]["replacement"] == "familiar"
    assert result["decisions"][0]["action"] == "apply"
    folder = next(caller.directory.glob("calls/*"))
    raw = (folder / "attempts/1/response.json").read_bytes()
    assert json.loads(raw)["result"]["parsed"]["decisions"] == decisions
    receipt = json.loads((folder / "receipt.json").read_text())
    assert receipt["decision_normalization"]["sites"][0]["proposal_ids"] == [p["id"] for p in site["proposals"]]
    assert caller.ask("typed_disputes", **kwargs) == result
    caller.assert_complete()
    assert len(provider.requests) == 1 and caller.usage_summary()["calls"] == 1
    assert (folder / "attempts/1/response.json").read_bytes() == raw


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unknown", "mixed", "stale_span", "outside_span", "wrong_paragraph"])
def test_nested_decision_normalization_never_invents_missing_coverage_or_source(tmp_path, defect):
    site, decisions, kwargs = dispute_fixture()
    if defect == "missing":
        decisions.pop()
    elif defect == "duplicate":
        decisions[1] = dict(decisions[0])
    elif defect == "unknown":
        decisions[0]["id"] = "f-unknown"
    elif defect == "mixed":
        decisions[0]["id"] = "d-site"
    elif defect == "stale_span":
        site["proposals"][0]["before"] = "x"
    elif defect == "outside_span":
        site["proposals"][0]["end"] = 100
    else:
        site["proposals"][0]["para_id"] = "other"
    kwargs["user"] = json.dumps({"sites": [site]})
    provider = FakeProvider([replace(GOOD, parsed={"decisions": decisions})])
    caller = calls(tmp_path, provider, max_attempts=1)
    for run in (caller, calls(tmp_path, provider)):
        with pytest.raises(fc.FixedCallError, match="invalid_coverage.*exhausted"):
            run.ask("typed_disputes", **kwargs)
    assert len(provider.requests) == 1 and caller.usage_summary()["calls"] == 1


@pytest.mark.parametrize("defect", ["novel_replacement", "conflict", "query", "format"])
def test_complete_but_ambiguous_nested_dispositions_drop_site(tmp_path, defect):
    site, decisions, kwargs = dispute_fixture()
    if defect == "novel_replacement":
        decisions[0]["replacement"] = "A whole paragraph cannot replace a hyphen."
    elif defect == "query":
        decisions[0].update(action="query", question="Who?", missing_knowledge="Identity")
    elif defect == "format":
        site["proposals"][0]["format"] = "italic"
    else:
        decisions[1].update(action="apply", replacement=site["proposals"][1]["replacement"])
    kwargs["user"] = json.dumps({"sites": [site]})
    provider = FakeProvider([replace(GOOD, parsed={"decisions": decisions})])
    caller = calls(tmp_path, provider)
    result = caller.ask("typed_disputes", **kwargs)["decisions"]
    assert result[0]["action"] == "drop" and result[0]["question"] == ""
    assert len(provider.requests) == 1
    receipt = json.loads(next(caller.directory.glob("calls/*/receipt.json")).read_text())
    assert receipt["decision_normalization"]["sites"][0]["rejected_ambiguous"] is True


def test_exhausted_nested_decisions_revalidate_without_resubmission_or_budget_reset(tmp_path, monkeypatch):
    import galley.fixed_decisions as adapter
    _, decisions, kwargs = dispute_fixture()
    provider = FakeProvider([replace(GOOD, parsed={"decisions": decisions})])
    real = adapter.grouped_decisions
    monkeypatch.setattr(adapter, "grouped_decisions", lambda *args: None)
    caller = calls(tmp_path, provider)
    with pytest.raises(fc.FixedCallError, match="invalid_coverage.*exhausted"):
        caller.ask("typed_disputes", **kwargs)
    folder = next(caller.directory.glob("calls/*"))
    frozen = {p: p.read_bytes() for p in folder.rglob("*.json") if p.name in {"response.json", "request.json", "coverage.json"}}
    usage = caller.usage_summary()
    monkeypatch.setattr(adapter, "grouped_decisions", real)
    resumed = calls(tmp_path, provider)
    result = resumed.ask("typed_disputes", **kwargs)
    assert result["decisions"][0]["replacement"] == "familiar"
    assert len(provider.requests) == 3
    assert all(p.read_bytes() == data for p, data in frozen.items())
    assert resumed.usage_summary()["calls"] == usage["calls"] == 3
    assert resumed.usage_summary()["output_tokens"] == usage["output_tokens"] == 30
    assert resumed.usage_summary()["limits"] == usage["limits"]
    receipt = json.loads((folder / "receipt.json").read_text())
    assert receipt["attempt"] == receipt["max_attempts"] == 3
    assert receipt["status"] == "completed"
    resumed.assert_complete()


def test_dispute_normalization_audit_cannot_be_changed_on_replay(tmp_path):
    _, decisions, kwargs = dispute_fixture()
    provider = FakeProvider([replace(GOOD, parsed={"decisions": decisions})])
    caller = calls(tmp_path, provider)
    caller.ask("typed_disputes", **kwargs)
    path = next(caller.directory.glob("calls/*/receipt.json"))
    receipt = json.loads(path.read_text())
    receipt["decision_normalization"]["normalized_sha256"] = "changed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(fc.FixedCallContractError, match="normalization changed"):
        caller.ask("typed_disputes", **kwargs)
    assert len(provider.requests) == 1
