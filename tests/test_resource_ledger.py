"""Real concurrent persistence and saved-usage parsing; never model calls."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from docproof import resource_ledger as rl
from docproof.providers.base import ProviderResult
from docproof.providers.openai_provider import result_from_response


USAGE = {"input_tokens": 10, "output_tokens": 4, "thinking_tokens": 2,
         "cache_creation_input_tokens": 3, "cache_read_input_tokens": 20}


def configured(monkeypatch, path):
    for key, value in rl.context_env(path, "source", "config", parent_operation_id="phase").items():
        monkeypatch.setenv(key, value)


def test_concurrent_idempotent_receipts_and_reused_work(tmp_path):
    path = tmp_path / "book.jsonl"
    def write(i):
        fields = dict(path=path, source_sha256="source", config_sha256="config",
                      receipt_id=str(i % 20), operation_id=str(i % 20),
                      model="sonnet", transport="claude_subscription")
        rl.append_usage(**fields, status="started")
        rl.append_usage(**fields, usage=USAGE)
        rl.append_usage(**fields, usage=USAGE, reused=True)
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, range(100)))
    result = rl.summarize(path)
    assert result["new_attempts"] == 20
    assert result["reused_receipts"] == 20
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 80
    assert result["thinking_tokens"] == 40  # subset, never added to output
    assert result["complete"]
    assert len(path.read_text().splitlines()) == 60


def test_unknown_usage_and_explicit_inclusive_parent_do_not_double_count(tmp_path):
    path = tmp_path / "book.jsonl"
    common = dict(path=path, source_sha256="s", config_sha256="c",
                  model="opus", transport="claude_subscription")
    rl.append_usage(**common, receipt_id="child", operation_id="child", usage=USAGE,
                    parent_operation_id="parent")
    rl.append_usage(**common, receipt_id="parent", operation_id="parent", usage=USAGE,
                    included_operation_ids=["child"])
    rl.append_usage(**common, receipt_id="failed", operation_id="failed", status="error")
    summary = rl.summarize(path)
    assert summary["input_tokens"] == 10
    assert summary["excluded_inclusive_children"] == 1
    assert summary["unknown_usage"] == 1
    assert not summary["complete"]
    assert json.loads(path.read_text().splitlines()[-1])["usage"] is None


def test_incomplete_reused_only_and_source_mismatch(tmp_path):
    path = tmp_path / "book.jsonl"
    args = dict(path=path, source_sha256="s", config_sha256="c", receipt_id="r",
                operation_id="op", model="m", transport="t")
    rl.append_usage(**args, usage=USAGE, reused=True)
    assert rl.summarize(path)["new_attempts"] == 0
    rl.append_usage(**args, status="started")
    assert rl.summarize(path)["incomplete_attempts"] == 1
    with pytest.raises(ValueError, match="another source"):
        rl.append_usage(**{**args, "source_sha256": "changed"}, usage=USAGE)
    path.write_text(path.read_text() + '{"partial"')
    with pytest.raises(ValueError, match="corrupt"):
        rl.summarize(path)


def test_saved_claude_result_uses_models_not_overlapping_rollup(monkeypatch, tmp_path):
    path = tmp_path / "book.jsonl"
    configured(monkeypatch, path)
    # Saved Bradshaw ladder shape: top usage omits native Sonnet subagent.
    result = {"subtype": "success", "usage": {"input_tokens": 999999},
              "modelUsage": {
                  "claude-fable-5-1": {"inputTokens": 932, "outputTokens": 54847,
                      "thinkingTokens": 24985, "cacheReadInputTokens": 2849700,
                      "cacheCreationInputTokens": 142678},
                  "claude-sonnet-5": {"inputTokens": 42, "outputTokens": 39515,
                      "thinkingTokens": 26492, "cacheReadInputTokens": 476382,
                      "cacheCreationInputTokens": 161540}}}
    for _ in range(2):
        rl.record_claude_result(result, operation_id="ladder", receipt_id="run1")
    totals = rl.summarize(path)
    assert totals["input_tokens"] == 974
    assert totals["output_tokens"] == 94362
    assert totals["thinking_tokens"] == 51477
    assert totals["cache_read_input_tokens"] == 3326082
    assert totals["new_attempts"] == 1


def test_api_raw_usage_records_actual_model_and_reasoning(monkeypatch, tmp_path):
    path = tmp_path / "book.jsonl"
    configured(monkeypatch, path)
    response = result_from_response({"id": "response-1", "model": "luna-version",
        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 30},
                  "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 12}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]})
    class Provider:
        name = "openai"
        def complete_structured(self, **kwargs):
            return response
    rl.metered(Provider(), effort="low").complete_structured(
        model="luna", max_tokens=100, schema_name="detector")
    summary = rl.summarize(path)
    assert summary["new_attempts"] == 1
    assert summary["input_tokens"] == 70
    assert summary["cache_read_input_tokens"] == 30
    assert summary["thinking_tokens"] == 12
    assert summary["by_model"]["luna-version"]["output_tokens"] == 40


def test_failed_provider_and_missing_bindings_are_not_zero_cost_success(monkeypatch, tmp_path):
    path = tmp_path / "book.jsonl"
    configured(monkeypatch, path)
    class Broken:
        name = "openai"
        def complete_structured(self, **kwargs):
            raise RuntimeError("failed")
    with pytest.raises(RuntimeError):
        rl.metered(Broken()).complete_structured(model="luna", max_tokens=100)


def test_atomic_output_reservations_survive_restart_and_missing_usage(monkeypatch, tmp_path):
    path = tmp_path / "book.jsonl"
    configured(monkeypatch, path)
    monkeypatch.setenv(rl.GROUP_ENV, "review")
    monkeypatch.setenv(rl.MAX_CALLS_ENV, "4")
    monkeypatch.setenv(rl.MAX_OUTPUT_ENV, "100")
    def reserve(i):
        try:
            rl.append_usage(receipt_id=str(i), operation_id=str(i), model="opus",
                transport="claude_subscription", status="started", max_output_tokens=60)
            return True
        except rl.ResourceBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(results) == 1
    winner = str(results.index(True))
    rl.append_usage(receipt_id=winner, operation_id=winner, model="opus",
                    transport="claude_subscription", status="error", usage=None)
    assert rl.summarize(path)["groups"]["review"]["reserved_output_tokens"] == 60
    # Persisted limits still apply after the caller loses its limit env.
    monkeypatch.delenv(rl.MAX_OUTPUT_ENV)
    monkeypatch.delenv(rl.MAX_CALLS_ENV)
    assert not reserve("after-restart")
    # Once real terminal usage is available, only the measured output counts.
    rl.append_usage(receipt_id=winner, operation_id=winner, model="opus",
                    transport="claude_subscription", usage={**USAGE, "output_tokens": 20})
    assert reserve("after-reconciliation")
    assert rl.summarize(path)["groups"]["review"]["charged_output_tokens"] == 80


def test_call_budget_is_shared_by_distinct_provider_instances(monkeypatch, tmp_path):
    path = tmp_path / "book.jsonl"
    configured(monkeypatch, path)
    monkeypatch.setenv(rl.GROUP_ENV, "review")
    monkeypatch.setenv(rl.MAX_CALLS_ENV, "1")
    class Provider:
        name = "openai"
        def complete_structured(self, **kwargs):
            return ProviderResult(parsed={})
    rl.metered(Provider()).complete_structured(model="luna", max_tokens=100)
    with pytest.raises(rl.ResourceBudgetExceeded, match="call budget"):
        rl.metered(Provider()).complete_structured(model="luna", max_tokens=100)
    assert rl.summarize(path)["unknown_usage"] == 1
    monkeypatch.delenv(rl.SOURCE_ENV)
    with pytest.raises(ValueError, match="requires source"):
        rl.metered(Provider()).complete_structured(model="luna", max_tokens=100)


def test_contexts_isolate_simultaneous_books_without_environment_changes(tmp_path):
    import os
    before = dict(os.environ)
    def book(i):
        path = tmp_path / f"book-{i}.jsonl"
        with rl.use_context(rl.context_env(path, f"source-{i}", "config")):
            rl.append_usage(receipt_id="r", operation_id="op", model="m", transport="t", usage=USAGE)
        return path
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(book, range(4)))
    assert dict(os.environ) == before
    for i, path in enumerate(paths):
        assert json.loads(path.read_text())["source_sha256"] == f"source-{i}"


def test_provider_retains_context_when_fanned_out_to_threads(tmp_path):
    path = tmp_path / "book.jsonl"
    env = {**rl.context_env(path, "source", "config"), rl.MAX_CALLS_ENV: "1"}
    class Provider:
        name = "openai"
        def complete_structured(self, **kwargs):
            return ProviderResult(parsed={})
    with rl.use_context(env):
        provider = rl.metered(Provider())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(provider.complete_structured, model="luna", max_tokens=100).result()
        with pytest.raises(rl.ResourceBudgetExceeded):
            pool.submit(provider.complete_structured, model="luna", max_tokens=100).result()
    assert rl.summarize(path)["groups"]["book"]["calls"] == 1


def test_inclusive_rollup_preserves_reservation_until_parent_output_known(tmp_path):
    path = tmp_path / "book.jsonl"
    common = dict(path=path, source_sha256="source", config_sha256="config", model="m", transport="t")
    rl.append_usage(**common, receipt_id="child", operation_id="child", status="started", max_output_tokens=100)
    rl.append_usage(**common, receipt_id="parent", operation_id="parent", usage={"input_tokens": 5},
                    included_operation_ids=["child"])
    assert rl.summarize(path)["groups"]["book"]["reserved_output_tokens"] == 100
    rl.append_usage(**common, receipt_id="parent", operation_id="parent", usage=USAGE,
                    included_operation_ids=["child"])
    group = rl.summarize(path)["groups"]["book"]
    assert group["charged_output_tokens"] == USAGE["output_tokens"]
    assert group["calls"] == 1
    with pytest.raises(ValueError, match="acyclic"):
        rl.append_usage(**common, receipt_id="child", operation_id="child", usage=USAGE,
                        included_operation_ids=["parent"])
    with pytest.raises(ValueError, match="acyclic"):
        rl.append_usage(**common, receipt_id="self", operation_id="self", usage=USAGE,
                        included_operation_ids=["self"])
