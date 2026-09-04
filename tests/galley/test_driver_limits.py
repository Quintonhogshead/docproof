"""The driver's ceilings: the API cap frozen into approval.json, the runaway
caps on each subscription session, and the settle policy — all with the fake
spawner, so nothing is spawned and nothing is spent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from galley import driver as gd
from .test_driver import (FIXTURE, FakeSpawner, MECH_PLAN, _deliverable,
                          _driver, _plan)


@pytest.fixture()
def book(tmp_path) -> Path:
    dest = tmp_path / "Ford - Book 1.docx"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def _ws(book: Path, tmp_path: Path) -> Path:
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, MECH_PLAN)
    return ws


# --- the API ceiling ---------------------------------------------------------

def test_the_house_defaults_are_the_owners_figures():
    assert gd.DEFAULT_BUDGET_USD == 10.0
    assert gd.DEFAULT_MODEL == "claude-fable-5-1"


def test_the_approve_prompt_freezes_the_drivers_cap_not_the_plan_total():
    prompt = gd.phase_prompt("approve", "B.docx", budget_usd=10.0)
    assert "--budget 10.00" in prompt
    assert "The cap is $10.00 of API spend" in prompt
    assert "that exact figure, not the plan's total" in prompt
    assert "--budget 4.00" in gd.phase_prompt("approve", "B.docx",
                                              budget_usd=4.0)


def test_the_model_reaches_the_command_line(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    _driver(book, tmp_path, spawn=spawn).run()
    argv = spawn.calls[0].argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"


def test_a_paid_verb_refusing_over_the_cap_stops_the_run(book, tmp_path):
    """`review --approval` exits 5 on `budget_over_cap`; the driver must stop
    there as needs_human rather than carry on to the next phase."""
    ws = _ws(book, tmp_path)

    class OverCap(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "ladder":
                self.calls.append(spec)
                spec.log_path.parent.mkdir(parents=True, exist_ok=True)
                spec.log_path.write_text(
                    "REFUSED: this run deviates from approval.json —\n"
                    "  - budget_over_cap: planned spend $14.00 exceeds the "
                    "approved $10.00\n", encoding="utf-8")
                return gd.PhaseResult(spec.phase, 5, spec.log_path,
                                      gd.tail_of(spec.log_path))
            return super().__call__(spec)

    spawn = OverCap(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "ladder"
    assert "budget_over_cap" in result.reason
    assert spawn.phases == ["profile", "approve", "sweeps", "ladder"]


# --- runaway protection ------------------------------------------------------

def test_each_phase_carries_its_turn_and_wall_clock_cap(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    _driver(book, tmp_path, spawn=spawn).run()
    caps = {c.phase: c.max_turns for c in spawn.calls}
    assert caps["settle"] == gd.PHASE_MAX_TURNS["settle"] == 400
    assert caps["verify"] == 250
    assert caps["certify"] == 60
    for call in spawn.calls:
        assert call.argv[call.argv.index("--max-turns") + 1] == \
            str(call.max_turns)
    timeouts = {c.phase: c.timeout_s for c in spawn.calls}
    assert timeouts["settle"] == 4 * 3600.0
    assert timeouts["verify"] == 4 * 3600.0
    assert timeouts["ladder"] == 3 * 3600.0
    assert timeouts["certify"] == gd.DEFAULT_PHASE_TIMEOUT_S


def test_the_caps_are_overridable_flatly_and_per_phase(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    drv = _driver(book, tmp_path, spawn=spawn, max_turns=7,
                  max_turns_by_phase={"settle": 999}, timeout_s=60.0,
                  timeout_by_phase={"verify": 90.0})
    assert drv.turns_for("ladder") == 7
    assert drv.turns_for("settle") == 999
    assert drv.timeout_for("ladder") == 60.0
    assert drv.timeout_for("verify") == 90.0
    drv.run()
    assert {c.phase: c.max_turns for c in spawn.calls}["settle"] == 999


def test_a_timed_out_phase_ends_the_run_naming_the_cap(book, tmp_path):
    ws = _ws(book, tmp_path)

    class Hangs(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "ladder":
                self.calls.append(spec)
                spec.log_path.parent.mkdir(parents=True, exist_ok=True)
                spec.log_path.write_text("thinking...\n", encoding="utf-8")
                return gd.PhaseResult(spec.phase, gd.TIMEOUT_RC, spec.log_path,
                                      "thinking...", limit="timeout")
            return super().__call__(spec)

    result = _driver(book, tmp_path, spawn=Hangs(ws)).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "ladder"
    assert "wall-clock cap of 3.0h" in result.reason


def test_a_phase_at_its_turn_cap_ends_the_run(book, tmp_path):
    ws = _ws(book, tmp_path)

    class Loops(FakeSpawner):
        def __call__(self, spec):
            out = super().__call__(spec)
            if spec.phase == "sweeps":
                return gd.PhaseResult(spec.phase, 0, out.log_path, out.tail,
                                      limit="max_turns")
            return out

    result = _driver(book, tmp_path, spawn=Loops(ws)).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "sweeps"
    assert "turn cap of 120" in result.reason


def test_the_turn_cap_is_read_off_the_log():
    assert gd.detect_turn_cap("Reached max turns (100)")
    assert gd.detect_turn_cap("error: maximum turns exceeded")
    assert not gd.detect_turn_cap("wrote runs/ladder/findings.json")


# --- the settle policy -------------------------------------------------------

def _settlement(ws: Path, *, stopped: str, last_new: int, rounds: int,
                quiet: bool) -> None:
    run = ws / "runs" / "final"
    run.mkdir(parents=True, exist_ok=True)
    (run / "findings.json").write_text(json.dumps(
        {"findings": [], "cost": {"total_usd": 0.0}}), encoding="utf-8")
    (run / "settlement.json").write_text(json.dumps(
        {"rounds": rounds, "records": [], "open": [],
         "convergence": {"stopped": stopped, "last_new_items": last_new,
                         "last_reread": 60, "rounds": rounds,
                         "quiet": quiet}}), encoding="utf-8")


def test_the_settle_flags_are_the_owners_policy():
    assert gd.SETTLE_ROUNDS == 3
    assert gd.SETTLE_QUIET_FLOOR == 4        # inclusive: "fewer than five"
    assert gd.SETTLE_QUIET_SHARE == 0.0      # the percentage rule is off
    assert gd.settle_flags() == ("--until-clean --rounds 3 --quiet-floor 4 "
                                 "--quiet-share 0")
    prompt = gd.phase_prompt("settle", "B.docx")
    assert "--until-clean --rounds 3 --quiet-floor 4 --quiet-share 0" in prompt
    assert "at most 3 round(s)" in prompt
    assert "fewer than 5 new item(s) is quiet" in prompt


def test_a_quiet_settle_round_finishes_the_book(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)

    class Settles(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "settle":
                _settlement(self.workspace, stopped="quiet", last_new=4,
                            rounds=3, quiet=True)
            return super().__call__(spec)

    spawn = Settles(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert spawn.phases == list(gd.MECHANICAL_PHASES)


def test_a_still_noisy_third_round_ends_the_run_as_needs_human(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)

    class NeverSettles(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "settle":
                _settlement(self.workspace, stopped="round_cap", last_new=9,
                            rounds=3, quiet=False)
            return super().__call__(spec)

    spawn = NeverSettles(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "settle"
    assert "still finding errors after 3 round(s): 9 in the last round" \
        in result.reason
    assert "needs a human proofreader" in result.reason
    assert spawn.phases[-1] == "settle"       # certify/deliver never ran
    outcome = json.loads((ws / "runs" / "outcome.json").read_text("utf-8"))
    assert "still finding errors after 3 round(s)" in outcome["reason"]
    # …and the decision log carries the same reason.
    log = (ws / "deliverable" / gd.DECISION_LOG_NAME).read_text("utf-8")
    assert "still finding errors after 3 round(s)" in log


def test_the_settle_numbers_are_driver_options(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    assert "--rounds 5 --quiet-floor 9 --quiet-share 0.05" in \
        gd.phase_prompt("settle", "B.docx", settle_rounds=5,
                        settle_quiet_floor=9, settle_quiet_share=0.05)

    class Settles(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "settle":
                _settlement(self.workspace, stopped="round_cap", last_new=8,
                            rounds=5, quiet=False)
            return super().__call__(spec)

    result = _driver(book, tmp_path, spawn=Settles(ws), settle_rounds=5,
                     settle_quiet_floor=9).run()
    assert result.outcome == "needs_human"
    # The reason quotes the floor this run was given, not the house default.
    assert "under 10 new items" in result.reason
    assert "after 5 round(s): 8 in the last round" in result.reason


def test_a_run_with_no_settlement_file_is_not_judged(book, tmp_path):
    """No settlement.json means settle never wrote one — the state gate
    catches that; the policy check must not invent a verdict."""
    ws = _ws(book, tmp_path)
    assert _driver(book, tmp_path, spawn=FakeSpawner(ws)).settle_verdict() == ""


# --- the decision log ships --------------------------------------------------

def test_a_finished_run_ships_a_decision_log(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    result = _driver(book, tmp_path, spawn=FakeSpawner(ws),
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "done", result.reason
    log = ws / "deliverable" / gd.DECISION_LOG_NAME
    assert log.is_file()
    text = log.read_text("utf-8")
    assert text.startswith("# Decision log — Ford - Book 1.docx")
    assert "**Plan gate (auto): approved**" in text
    assert (tmp_path / "handoff" / "Ford - Book 2 - decision-log.md").is_file()
