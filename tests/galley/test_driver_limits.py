"""The driver's ceilings: the API cap frozen into approval.json, the runaway
caps on each subscription session, and the settle policy — all with the fake
spawner, so nothing is spawned and nothing is spent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from galley import driver as gd
from .test_driver import (FIXTURE, FakeSpawner, LEGACY_PHASES, MECH_PLAN, _deliverable,
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


def _argv_by_phase(spawn):
    return {call.phase: call.argv for call in spawn.calls}


def _flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def test_the_brains_are_split_by_phase(book, tmp_path):
    """Owner, 2026-09-06: Opus 5 drives the scripted phases, Fable 5.1 at
    high effort the judgment phases."""
    assert gd.MECHANICAL_MODEL == "claude-opus-5"
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    _driver(book, tmp_path, spawn=spawn).run()
    argv = _argv_by_phase(spawn)
    for phase in ("profile", "sweeps", "verify", "certify", "deliver"):
        assert _flag(argv[phase], "--model") == "claude-opus-5", phase
        assert _flag(argv[phase], "--effort") is None, phase
    for phase in ("approve", "ladder", "audit", "settle"):
        assert _flag(argv[phase], "--model") == "claude-fable-5-1", phase
        assert _flag(argv[phase], "--effort") == "high", phase


def test_a_global_model_or_effort_overrides_the_table(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    _driver(book, tmp_path, spawn=spawn, model="claude-sonnet-5",
            effort="medium").run()
    for phase, argv in _argv_by_phase(spawn).items():
        assert _flag(argv, "--model") == "claude-sonnet-5", phase
        assert _flag(argv, "--effort") == "medium", phase


def test_a_per_phase_model_or_effort_wins_over_everything(book, tmp_path):
    ws = _ws(book, tmp_path)
    _deliverable(ws)
    spawn = FakeSpawner(ws)
    _driver(book, tmp_path, spawn=spawn, model="claude-sonnet-5",
            model_by_phase={"settle": "claude-fable-5-1"},
            effort_by_phase={"settle": "max", "profile": "low"}).run()
    argv = _argv_by_phase(spawn)
    assert _flag(argv["settle"], "--model") == "claude-fable-5-1"
    assert _flag(argv["settle"], "--effort") == "max"
    assert _flag(argv["profile"], "--model") == "claude-sonnet-5"
    assert _flag(argv["profile"], "--effort") == "low"
    assert _flag(argv["sweeps"], "--effort") is None


def test_an_unknown_effort_level_is_refused():
    drv = gd.Driver(book=Path("B.docx"), slug="b", effort="turbo")
    with pytest.raises(gd.DriverError, match="turbo"):
        drv.effort_for("settle")


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


def test_the_turn_cap_is_read_off_the_transcript_not_the_header():
    assert gd.detect_turn_cap("Reached max turns (100)")
    assert gd.detect_turn_cap("error: maximum turns exceeded")
    assert not gd.detect_turn_cap("wrote runs/ladder/findings.json")
    # GALLEY-003: the driver's own header names the cap; that is not the
    # session hitting it.
    assert not gd.detect_turn_cap(
        "# galley driver: phase sweeps at t (max-turns 120, timeout 2.0h)\n"
        "wrote runs/sweeps.txt")


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
    assert spawn.phases == LEGACY_PHASES


def test_a_still_noisy_third_round_is_needs_human_but_still_delivers(
        book, tmp_path):
    """A book that will not converge is needs_human — and still ships. The
    proofreader who picks it up needs the manuscript Galley edited, not a
    verdict about it, so certify and deliver run and the hand-off is whole."""
    ws = _ws(book, tmp_path)
    _deliverable(ws)

    class NeverSettles(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "settle":
                _settlement(self.workspace, stopped="round_cap", last_new=9,
                            rounds=3, quiet=False)
            return super().__call__(spec)

    spawn = NeverSettles(ws)
    result = _driver(book, tmp_path, spawn=spawn,
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "needs_human"
    assert result.stopped_at is None          # not a stop: a verdict
    assert "still finding errors after 3 round(s): 9 in the last round" \
        in result.reason
    assert "needs a human proofreader" in result.reason
    assert spawn.phases == LEGACY_PHASES   # certify/deliver ran
    # The whole hand-off, with the verdict in it.
    names = {p.name for p in result.handoff}
    assert "Ford - Book 2.docx" in names
    assert "Ford - Book 2 - clean.docx" in names
    assert "Ford - Book 2 - outcome.json" in names
    outcome = json.loads(
        (tmp_path / "handoff" / "Ford - Book 2 - outcome.json"
         ).read_text("utf-8"))
    assert outcome["outcome"] == "needs_human"
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


def test_profile_has_room_for_a_from_scratch_run():
    """Raised from 80 on 2026-09-07: a clean-workspace profile on a 65k-word
    book spent all 80 turns on the scans, the genre pack, the egress report
    and two pricing passes, and never reached PLAN.md — so the cap threw away
    every bit of it. The phases that fit in 80 were resuming workspaces whose
    scans already existed."""
    from galley.driver import PHASE_MAX_TURNS

    assert PHASE_MAX_TURNS["profile"] >= 160
    # It writes the plan the whole run is gated on; it should not be the
    # tightest budget on the board.
    assert PHASE_MAX_TURNS["profile"] > PHASE_MAX_TURNS["approve"]


# --- caps that grow with the book ------------------------------------------

def _drv(tmp_path, **kw):
    return gd.Driver(book=tmp_path / "b.docx", slug="s",
                     workspace_root=tmp_path / "ws", **kw)


def test_length_factor_floors_at_one_and_caps_at_four():
    assert gd.length_factor(None) == 1.0
    assert gd.length_factor(0) == 1.0
    assert gd.length_factor(3_614) == 1.0            # the Gull Point story
    assert gd.length_factor(gd.LENGTH_BASELINE_WORDS) == 1.0
    assert gd.length_factor(65_000) == pytest.approx(1.3)
    assert gd.length_factor(100_000) == pytest.approx(2.0)
    assert gd.length_factor(235_898) == gd.LENGTH_SCALE_MAX   # Reaves, not 4.7
    assert gd.length_factor("garbage") == 1.0


def test_only_the_phases_whose_work_grows_with_the_book_scale(tmp_path):
    drv = _drv(tmp_path, words=100_000)
    for phase in gd.LENGTH_SCALED_PHASES:
        assert drv.turns_for(phase) == gd.PHASE_MAX_TURNS[phase] * 2, phase
        assert drv.timeout_for(phase) == gd.PHASE_TIMEOUT_S[phase] * 2, phase
    # Fixed-overhead phases: profile took 17 minutes on 65k words and on 3.6k.
    for phase in ("profile", "approve", "sweeps", "audit", "certify", "deliver"):
        assert drv.turns_for(phase) == gd.PHASE_MAX_TURNS[phase], phase
        assert drv.timeout_for(phase) == gd.PHASE_TIMEOUT_S.get(
            phase, gd.DEFAULT_PHASE_TIMEOUT_S), phase


def test_the_word_count_comes_off_the_workspace_profile(tmp_path):
    drv = _drv(tmp_path)
    assert drv.book_words() is None                  # profile has not run
    assert drv.timeout_for("verify") == gd.PHASE_TIMEOUT_S["verify"]
    drv.workspace.mkdir(parents=True)
    (drv.workspace / "profile.json").write_text(
        '{"word_count": 150000, "comment_budget": 150}', encoding="utf-8")
    assert drv.book_words() == 150_000
    assert drv.timeout_for("verify") == gd.PHASE_TIMEOUT_S["verify"] * 3
    assert drv.turns_for("settle") == gd.PHASE_MAX_TURNS["settle"] * 3
    # An explicit figure wins over the file.
    drv.words = 10_000
    assert drv.timeout_for("verify") == gd.PHASE_TIMEOUT_S["verify"]


def test_an_explicit_cap_is_taken_exactly_never_scaled(tmp_path):
    drv = _drv(tmp_path, words=200_000, timeout_s=3600.0,
               max_turns_by_phase={"verify": 30})
    assert drv.timeout_for("verify") == 3600.0
    assert drv.turns_for("verify") == 30
    assert drv.turns_for("settle") == gd.PHASE_MAX_TURNS["settle"] * 4


def test_the_banner_says_when_and_why_a_phase_was_stretched(tmp_path):
    drv = _drv(tmp_path, words=82_000)
    assert drv.length_factor_for("verify") == pytest.approx(1.64)
    assert drv.length_factor_for("profile") == 1.0
