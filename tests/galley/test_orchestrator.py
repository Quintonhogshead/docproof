"""D2 — the wave loop: budget honored, waves recorded, deterministic, resumable."""

import pytest

from galley.casefile import CaseFile
from galley.contracts import Hypothesis
from galley.orchestrator import Dispatch, caps_for_tier, run_galley

from tests.galley.fakes import FakeDetector, gfinding, make_manuscript

FIXED_CLOCK = lambda: "2026-08-21T00:00:00Z"  # noqa: E731


def _ms():
    return make_manuscript("alpha here", "beta here", "gamma here")


def _ladder(cost=0.5):
    return FakeDetector(
        name="docproof_ladder",
        scripted=[
            gfinding("g-1", "body-0001", "alpha", "Alpha"),
            gfinding("g-2", "body-0002", "beta", "Beta"),
        ],
        cost_usd=cost,
    )


def test_default_stubs_run_ladder_then_stop(tmp_path):
    ladder = _ladder()
    cf = run_galley(
        _ms(), "T0", budget_usd=10.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder}, clock=FIXED_CLOCK,
    )
    assert len(cf.waves) == 1
    assert cf.waves[0].index == 1
    assert {f.id for f in cf.findings} == {"g-1", "g-2"}
    assert cf.budget.spent_usd == 0.5
    assert cf.budget.spent_usd <= 10.0
    # Case file persisted.
    assert (tmp_path / "casefile.json").exists()
    assert len(ladder.calls) == 1


def test_multi_wave_with_scripted_planner(tmp_path):
    ladder = _ladder()
    reread = FakeDetector(
        name="reread",
        scripted=[
            gfinding("g-3", "body-0003", "gamma", "Gamma"),
            gfinding("g-1", "body-0001", "alpha", "Alpha"),  # duplicate id -> unioned out
        ],
        cost_usd=0.2,
    )

    def audit(cf, ms):
        return [Hypothesis(chapter=1, error_class="typo", why="dense")]

    def plan(hyps, gov, cf):
        # One extra wave, then dry.
        if len(cf.waves) < 2:
            return [Dispatch("reread", make_scope())]
        return []

    def make_scope():
        from galley.adapters import Scope
        return Scope()

    cf = run_galley(
        _ms(), "T2", budget_usd=10.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        audit=audit, plan_wave=plan, clock=FIXED_CLOCK,
    )
    assert [w.index for w in cf.waves] == [1, 2]
    assert {f.id for f in cf.findings} == {"g-1", "g-2", "g-3"}  # g-1 not double-added
    assert cf.waves[1].findings_added == 1  # only g-3 was new
    assert cf.hypotheses  # audit hypotheses recorded
    assert cf.budget.spent_usd == pytest.approx(0.7)  # 0.5 + 0.2


def test_budget_is_honored_no_overspend(tmp_path):
    ladder = _ladder(cost=0.6)
    reread = FakeDetector(name="reread", scripted=[gfinding("g-9", "body-0003", "gamma", "G")], cost_usd=0.6)

    def plan(hyps, gov, cf):
        if len(cf.waves) < 2:
            from galley.adapters import Scope
            return [Dispatch("reread", Scope())]
        return []

    cf = run_galley(
        _ms(), "T2", budget_usd=1.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        plan_wave=plan, clock=FIXED_CLOCK,
    )
    # Wave 1 spent 0.6; wave 2 needs 0.6 but only 0.4 remains -> declined, no charge.
    assert cf.budget.spent_usd == pytest.approx(0.6)
    assert cf.budget.spent_usd <= 1.0
    # The reread adapter was asked but declined for budget (no g-9 added).
    assert "g-9" not in {f.id for f in cf.findings}


def test_max_waves_caps_the_loop(tmp_path):
    ladder = _ladder()
    reread = FakeDetector(name="reread", scripted=[], cost_usd=0.1)

    def plan(hyps, gov, cf):
        from galley.adapters import Scope
        return [Dispatch("reread", Scope())]  # always asks for more

    cf = run_galley(
        _ms(), "T2", budget_usd=100.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        plan_wave=plan, clock=FIXED_CLOCK,
    )
    # T2 = max 2 waves, so even an always-hungry planner is capped.
    assert len(cf.waves) == 2


def test_kill_and_resume(tmp_path):
    ladder = _ladder()
    reread = FakeDetector(name="reread", scripted=[gfinding("g-3", "body-0003", "gamma", "G")], cost_usd=0.2)

    class CrashingPlanner:
        def __init__(self):
            self.calls = 0

        def __call__(self, hyps, gov, cf):
            self.calls += 1
            if self.calls == 1:
                from galley.adapters import Scope
                return [Dispatch("reread", Scope())]  # wave 2
            raise RuntimeError("simulated crash before wave 3")

    adapters = {"docproof_ladder": ladder, "reread": reread}

    # First run crashes after wave 2 is recorded and saved.
    with pytest.raises(RuntimeError):
        run_galley(
            _ms(), "T3", budget_usd=100.0, out_dir=tmp_path,
            adapters=adapters, plan_wave=CrashingPlanner(), clock=FIXED_CLOCK,
        )

    # The case file on disk is consistent through the last closed wave.
    disk = CaseFile.load(tmp_path / "casefile.json")
    assert [w.index for w in disk.waves] == [1, 2]
    assert len(ladder.calls) == 1  # ladder ran once

    # Resume: a dry planner. The ladder must NOT re-run; waves unchanged.
    cf = run_galley(
        _ms(), "T3", budget_usd=100.0, out_dir=tmp_path,
        adapters=adapters, plan_wave=lambda h, g, c: [], clock=FIXED_CLOCK,
    )
    assert [w.index for w in cf.waves] == [1, 2]
    assert len(ladder.calls) == 1  # still one — wave 1 was not re-run on resume


def test_deterministic_given_scripted_fakes(tmp_path):
    def run(where):
        return run_galley(
            _ms(), "T0", budget_usd=10.0, out_dir=where,
            adapters={"docproof_ladder": _ladder()}, clock=FIXED_CLOCK,
        ).to_json()

    a = run(tmp_path / "a")
    b = run(tmp_path / "b")
    assert a == b


def test_notify_fires_per_wave(tmp_path):
    events = []
    run_galley(
        _ms(), "T0", budget_usd=10.0, out_dir=tmp_path,
        adapters={"docproof_ladder": _ladder()},
        notify=lambda kind, payload: events.append((kind, payload)),
        clock=FIXED_CLOCK,
    )
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "wave"
    assert payload["wave"] == 1 and payload["findings_total"] == 2


def test_caps_for_tier():
    assert caps_for_tier("T0", 10.0).max_waves == 1
    assert caps_for_tier("T2", 50.0).max_waves == 2
    assert caps_for_tier("T3", 150.0).total_usd == 150.0
