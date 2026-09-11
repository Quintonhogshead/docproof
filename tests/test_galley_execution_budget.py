"""Reservations survive restarts and never turn missing receipts into credit."""
from __future__ import annotations

import json

import pytest

from galley.execution_budget import ExecutionBudget, ExecutionBudgetError


def _budget(tmp_path, source="source-sha"):
    return ExecutionBudget(tmp_path / "budget.json", source)


def test_unfinished_attempt_keeps_its_full_reservation_across_restart(tmp_path):
    budget = _budget(tmp_path)
    budget.reserve("verify", 10, 100, log_path=tmp_path / "attempt.log")
    resumed = _budget(tmp_path)
    assert resumed.remaining("verify", 10, 100) == (0, 0)
    with pytest.raises(ExecutionBudgetError, match="reconciliation"):
        resumed.reserve("verify", 10, 100, log_path=tmp_path / "next.log")


def test_finished_attempt_charges_actual_usage_without_resetting_the_original_cap(tmp_path):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve("verify", 10, 100, log_path=tmp_path / "first.log")
    budget.finish(key, turns=4, seconds=35, status="completed")
    resumed = _budget(tmp_path)
    assert resumed.remaining("verify", 100, 1000) == (6, 65)
    next_key, turns, seconds = resumed.reserve(
        "verify", 100, 1000, log_path=tmp_path / "next.log")
    assert (turns, seconds) == (6, 65)
    resumed.finish(next_key, turns=6, seconds=5, status="failed")
    with pytest.raises(ExecutionBudgetError, match="exhausted"):
        _budget(tmp_path).reserve("verify", 10, 100, log_path=tmp_path / "third.log")


def test_finishing_twice_cannot_refund_an_already_recorded_attempt(tmp_path):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve("verify", 10, 100, log_path=tmp_path / "first.log")
    budget.finish(key, turns=4, seconds=35, status="completed")
    budget.finish(key, turns=0, seconds=0, status="completed")
    assert budget.remaining("verify", 10, 100) == (6, 65)


def test_unknown_turn_usage_remains_charged_and_phases_have_separate_caps(tmp_path):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve("verify", 10, 100, log_path=tmp_path / "first.log")
    budget.finish(key, turns=None, seconds=12, status="failed")
    assert budget.remaining("verify", 10, 100) == (0, 88)
    assert budget.remaining("audit", 5, 40) == (5, 40)


def test_zero_turn_request_cannot_bypass_an_exhausted_durable_turn_limit(tmp_path):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve("verify", 10, 100, log_path=tmp_path / "first.log")
    budget.finish(key, turns=10, seconds=10, status="completed")
    with pytest.raises(ExecutionBudgetError):
        budget.reserve("verify", 0, 100, log_path=tmp_path / "second.log")
    data = json.loads(budget.path.read_text(encoding="utf-8"))
    assert all(row["turns"] >= 0 for row in data["attempts"])


def test_code_only_phase_can_use_a_seconds_budget_without_turns(tmp_path):
    budget = _budget(tmp_path)
    key, turns, seconds = budget.reserve("code-verify", 0, 100,
                                          log_path=tmp_path / "first.log")
    assert (turns, seconds) == (0, 100)
    budget.finish(key, turns=0, seconds=70, status="completed")
    assert _budget(tmp_path).remaining("code-verify", 0, 100) == (0, 30)


def test_budget_cannot_be_reused_for_another_source_revision(tmp_path):
    _budget(tmp_path).remaining("verify", 10, 100)
    with pytest.raises(ExecutionBudgetError, match="source revision"):
        _budget(tmp_path, "changed-source").remaining("verify", 10, 100)


def test_stale_completion_stream_cannot_refund_a_new_attempt(tmp_path):
    log = tmp_path / "verify.log"
    stale = {"num_turns": 1, "duration_ms": 1000}
    log.with_suffix(".stream.jsonl").write_text(json.dumps(stale), encoding="utf-8")
    budget = _budget(tmp_path)
    with pytest.raises(ExecutionBudgetError):
        budget.reserve("verify", 10, 100, log_path=log)
    assert budget.remaining("verify", 10, 100) == (10, 100)


def test_a_stream_older_than_the_reservation_does_not_release_it(tmp_path):
    import os

    log = tmp_path / "verify.log"
    budget = _budget(tmp_path)
    budget.reserve("verify", 10, 100, log_path=log)
    stream = log.with_suffix(".stream.jsonl")
    stream.write_text(json.dumps({"num_turns": 1, "duration_ms": 1000}), encoding="utf-8")
    os.utime(stream, ns=(1, 1))
    budget.reconcile(json.loads)
    assert budget.remaining("verify", 10, 100) == (0, 0)


def test_invalid_stream_receipt_does_not_release_a_reservation(tmp_path):
    log = tmp_path / "verify.log"
    budget = _budget(tmp_path)
    budget.reserve("verify", 10, 100, log_path=log)
    log.with_suffix(".stream.jsonl").write_text(
        json.dumps({"num_turns": 1, "duration_ms": -1}), encoding="utf-8")
    budget.reconcile(json.loads)
    assert budget.remaining("verify", 10, 100) == (0, 0)


def test_completed_attempt_can_be_reconciled_after_coordinator_restart(tmp_path):
    log = tmp_path / "verify.log"
    budget = _budget(tmp_path)
    budget.reserve("verify", 10, 100, log_path=log)
    log.with_suffix(".stream.jsonl").write_text(
        json.dumps({"num_turns": 3, "duration_ms": 12000}), encoding="utf-8")
    resumed = _budget(tmp_path)
    resumed.reconcile(json.loads)
    assert resumed.remaining("verify", 10, 100) == (7, 88)
    resumed.reconcile(json.loads)
    assert resumed.remaining("verify", 10, 100) == (7, 88)


def test_over_cap_usage_is_charged_in_full_not_clamped_to_the_reservation(tmp_path):
    log = tmp_path / "verify.log"
    budget = _budget(tmp_path)
    budget.reserve("verify", 10, 100, log_path=log)
    log.with_suffix(".stream.jsonl").write_text(
        json.dumps({"num_turns": 12, "duration_ms": 120000}), encoding="utf-8")
    budget.reconcile(json.loads)
    assert budget.remaining("verify", 10, 100) == (-2, -20)
    with pytest.raises(ExecutionBudgetError):
        budget.reserve("verify", 10, 100, log_path=tmp_path / "next.log")


def test_concurrent_callers_cannot_both_reserve_the_same_allowance(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def reserve(index):
        try:
            return _budget(tmp_path).reserve(
                "verify", 10, 100, log_path=tmp_path / f"attempt-{index}.log")
        except ExecutionBudgetError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(reserve, range(2)))
    assert sum(row is not None for row in reservations) == 1
    assert _budget(tmp_path).remaining("verify", 10, 100) == (0, 0)
