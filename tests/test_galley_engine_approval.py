"""The profile's structured ceiling must survive the real approval CLI."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from galley.engine_phases import EnginePhaseError, EnginePhases
from galley.manifest import sha256_file
from galley.state_machine import RunStateMachine
from tests.test_galley_engine_phases import _driver


LIVE_COMMENT_BUDGET = {
    "ceiling": 101,
    "basis": "about one per 1k over 101060 words",
    "comment_collapse": 3,
    "expected_use": "Use comments only for actionable author questions",
    "rule": "never raise ceiling",
}


def _profiled_driver(tmp_path, profile):
    driver = _driver(tmp_path)
    machine = RunStateMachine(source_sha256=sha256_file(driver.book))
    machine.advance("intake", source_sha256=machine.source_sha256)
    machine.save(tmp_path / "state.json")
    (tmp_path / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return driver


@pytest.mark.parametrize("budget,expected", [
    (LIVE_COMMENT_BUDGET, 101), (101, 101), (101.0, 101),
    ({"ceiling": 17.0, "basis": "profile decision"}, 17),
])
def test_approve_executes_cli_with_exact_profile_ceiling(tmp_path, budget, expected):
    from docproof.__main__ import main

    driver = _profiled_driver(tmp_path, {"word_count": 101060, "comment_budget": budget})
    profile_path = tmp_path / "profile.json"
    profile_before = profile_path.read_bytes()
    source_before = driver.book.read_bytes()
    calls = []

    def execute(spec):
        calls.append(spec)
        # Exercise argparse, approval manifest construction, and artifact writes;
        # approval is deterministic and never makes a provider call.
        code = main(spec.argv[1:])
        return SimpleNamespace(returncode=code, limit="", tail="")

    phases = EnginePhases(driver, execute)
    phases.approve()
    approval = json.loads((tmp_path / "approval.json").read_text())
    assert approval["comment_budget"] == expected
    assert type(approval["comment_budget"]) is int
    assert approval["source_sha256"] == sha256_file(driver.book)
    assert RunStateMachine.load(tmp_path / "state.json").current == "plan_approved"
    assert json.loads((phases.directory / "approve.json").read_text())["status"] == "completed"
    assert profile_path.read_bytes() == profile_before
    assert driver.book.read_bytes() == source_before
    # A valid saved approval advances/reuses normally; no profile or CLI repeat.
    phases.approve()
    assert len(calls) == 1


@pytest.mark.parametrize("budget", [
    None, True, False, 0, -1, 1.5, float("nan"), float("inf"), "101", [],
    {}, {"basis": "one per 1k"}, {"ceiling": None}, {"ceiling": True},
    {"ceiling": 0}, {"ceiling": -1}, {"ceiling": 101.5}, {"ceiling": "101"},
])
def test_approve_rejects_invalid_ceiling_before_launch_or_state_change(tmp_path, budget):
    driver = _profiled_driver(tmp_path, {"comment_budget": budget})
    before = (tmp_path / "state.json").read_bytes()
    phases = EnginePhases(driver, lambda spec: pytest.fail("invalid budget must not launch approval"))
    with pytest.raises(EnginePhaseError, match="comment_budget.*positive integer"):
        phases.approve()
    assert not (tmp_path / "approval.json").exists()
    assert not (phases.directory / "approve.json").exists()
    assert (tmp_path / "state.json").read_bytes() == before


def test_approve_without_stated_ceiling_preserves_existing_cli_contract(tmp_path):
    from docproof.__main__ import main

    driver = _profiled_driver(tmp_path, {"word_count": 101060})

    def execute(spec):
        assert "--comment-budget" not in spec.argv
        return SimpleNamespace(returncode=main(spec.argv[1:]), limit="", tail="")

    EnginePhases(driver, execute).approve()
    assert json.loads((tmp_path / "approval.json").read_text())["comment_budget"] is None
