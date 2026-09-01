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


# ---- the ledger is the truth: over-cap charges, estimates, interruptions ---

from dataclasses import dataclass, field  # noqa: E402

from docproof.models import Usage  # noqa: E402

from galley.adapters import AdapterResult, Scope  # noqa: E402
from galley.orchestrator import ALARM_BUDGET_OVERRUN  # noqa: E402


@dataclass
class OverspendingDetector(FakeDetector):
    """A detector that bills what it bills, budget or no budget — the real
    failure mode: the money is spent before anyone can decline."""

    wave: int = 0

    def run(self, ms, scope, budget_usd, usage):
        self.calls.append((scope, budget_usd))
        target = set(scope.paragraph_ids(ms))
        return AdapterResult(
            findings=[f for f in self.scripted if f.span.para_id in target],
            coverage_notes=list(self.coverage_notes),
            cost_usd=self.cost_usd,
        )


@dataclass
class EstimatingDetector(FakeDetector):
    """A FakeDetector that can price a dispatch up front."""

    estimate: float | None = None
    estimates: list[Scope] = field(default_factory=list)

    def estimate_usd(self, ms, scope):
        self.estimates.append(scope)
        return self.estimate


@dataclass
class CrashingDetector(FakeDetector):
    def run(self, ms, scope, budget_usd, usage):
        self.calls.append((scope, budget_usd))
        raise RuntimeError("simulated adapter crash mid-wave")


def _plan_once(*dispatches):
    """A planner that asks for `dispatches` in wave 2, then goes dry."""

    def plan(hyps, gov, cf):
        return list(dispatches) if len(cf.waves) < 2 else []

    return plan


def test_over_cap_charge_is_recorded_and_findings_kept(tmp_path):
    ladder = _ladder(cost=0.6)
    reread = OverspendingDetector(
        name="reread",
        scripted=[gfinding("g-9", "body-0003", "gamma", "G")], cost_usd=0.6)
    events = []
    cf = run_galley(
        _ms(), "T2", budget_usd=1.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        plan_wave=_plan_once(Dispatch("reread", Scope())),
        notify=lambda kind, payload: events.append((kind, payload)),
        clock=FIXED_CLOCK,
    )
    # The 0.6 really was spent: charged past the cap, findings kept, flagged.
    assert cf.budget.spent_usd == pytest.approx(1.2)
    assert "g-9" in {f.id for f in cf.findings}
    action = cf.waves[1].actions[0]
    assert action["over_cap"] is True and action["cost_usd"] == 0.6
    assert any("overrunning the budget cap" in n
               for n in action["coverage_notes"])
    alarms = [p for k, p in events if k == "alarm"]
    assert any(a["alarm"] == ALARM_BUDGET_OVERRUN for a in alarms)
    # Persisted, not just in memory.
    assert CaseFile.load(tmp_path / "casefile.json").budget.spent_usd == \
        pytest.approx(1.2)


def test_estimate_over_budget_skips_the_dispatch_before_it_runs(tmp_path):
    ladder = _ladder(cost=0.5)
    pricey = EstimatingDetector(
        name="pricey", scripted=[gfinding("g-9", "body-0003", "gamma", "G")],
        cost_usd=0.3, estimate=9.0)
    cheap = EstimatingDetector(
        name="cheap", scripted=[gfinding("g-8", "body-0002", "beta", "B")],
        cost_usd=0.2, estimate=0.2)
    cf = run_galley(
        _ms(), "T2", budget_usd=1.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "pricey": pricey, "cheap": cheap},
        plan_wave=_plan_once(Dispatch("pricey", Scope()),
                             Dispatch("cheap", Scope())),
        clock=FIXED_CLOCK,
    )
    assert pricey.calls == []                      # never ran, never charged
    assert len(cheap.calls) == 1                   # a later, cheaper one still fits
    assert cf.budget.spent_usd == pytest.approx(0.7)
    skipped, ran = cf.waves[1].actions
    assert skipped["skipped"] == "over budget (estimated)"
    assert skipped["estimate_usd"] == 9.0
    assert ran["estimate_usd"] == 0.2 and ran["cost_usd"] == 0.2
    assert "g-9" not in {f.id for f in cf.findings}


def test_adapter_exception_persists_the_ledger_mid_wave(tmp_path):
    ladder = _ladder()
    reread = FakeDetector(
        name="reread", scripted=[gfinding("g-3", "body-0003", "gamma", "G")],
        cost_usd=0.2)
    boom = CrashingDetector(name="boom")
    adapters = {"docproof_ladder": ladder, "reread": reread, "boom": boom}

    with pytest.raises(RuntimeError):
        run_galley(
            _ms(), "T3", budget_usd=100.0, out_dir=tmp_path, adapters=adapters,
            plan_wave=_plan_once(Dispatch("reread", Scope()),
                                 Dispatch("boom", Scope())),
            clock=FIXED_CLOCK,
        )
    disk = CaseFile.load(tmp_path / "casefile.json")
    # Wave 2 never closed, but what it bought before the crash is on disk.
    assert [w.index for w in disk.waves] == [1]
    assert disk.budget.spent_usd == pytest.approx(0.7)
    assert [c.wave for c in disk.budget.charges] == [1, 2]
    assert "g-3" in {f.id for f in disk.findings}


def test_resume_continues_an_interrupted_wave_without_rerunning_wave_one(tmp_path):
    ladder = _ladder()
    reread = FakeDetector(
        name="reread", scripted=[gfinding("g-3", "body-0003", "gamma", "G")],
        cost_usd=0.2)
    boom = CrashingDetector(name="boom")
    adapters = {"docproof_ladder": ladder, "reread": reread, "boom": boom}

    with pytest.raises(RuntimeError):
        run_galley(
            _ms(), "T3", budget_usd=100.0, out_dir=tmp_path, adapters=adapters,
            plan_wave=_plan_once(Dispatch("reread", Scope()),
                                 Dispatch("boom", Scope())),
            clock=FIXED_CLOCK,
        )

    # Resume: the crash is fixed (no boom dispatch). The interrupted wave is
    # continued under its own index; the ladder does not re-run.
    cf = run_galley(
        _ms(), "T3", budget_usd=100.0, out_dir=tmp_path, adapters=adapters,
        plan_wave=_plan_once(Dispatch("reread", Scope())), clock=FIXED_CLOCK,
    )
    assert len(ladder.calls) == 1
    assert [w.index for w in cf.waves] == [1, 2]
    assert cf.waves[1].actions[0]["adapter"] == "reread"
    # Both wave-2 charges stand (the fake has no checkpoint to replay from);
    # nothing was lost and nothing was double-counted in the findings.
    assert [c.wave for c in cf.budget.charges] == [1, 2, 2]
    assert cf.budget.spent_usd == pytest.approx(0.9)
    assert [f.id for f in cf.findings].count("g-3") == 1


def test_orchestrator_stamps_the_wave_on_adapters(tmp_path):
    ladder = OverspendingDetector(
        name="docproof_ladder",
        scripted=[gfinding("g-1", "body-0001", "alpha", "A")], cost_usd=0.1)
    reread = OverspendingDetector(
        name="reread", scripted=[gfinding("g-3", "body-0003", "gamma", "G")],
        cost_usd=0.1)
    run_galley(
        _ms(), "T2", budget_usd=10.0, out_dir=tmp_path,
        adapters={"docproof_ladder": ladder, "reread": reread},
        plan_wave=_plan_once(Dispatch("reread", Scope())), clock=FIXED_CLOCK,
    )
    assert ladder.wave == 1
    assert reread.wave == 2


def test_auditor_that_declares_a_governor_is_handed_one(tmp_path):
    seen = []

    def audit(cf, ms, governor=None):
        seen.append(governor)
        if governor is not None and not cf.hypotheses:
            governor.charge(0.05, f"audit:wave{governor.current_wave}",
                            allow_over_cap=True)
        return [Hypothesis(chapter=0, error_class="typo", why="x")] \
            if not cf.hypotheses else []

    cf = run_galley(
        _ms(), "T2", budget_usd=10.0, out_dir=tmp_path,
        adapters={"docproof_ladder": _ladder()}, audit=audit,
        plan_wave=lambda h, g, c: [], clock=FIXED_CLOCK,
    )
    assert seen and seen[0] is not None
    assert [c.label for c in cf.budget.charges] == \
        ["wave1:docproof_ladder", "audit:wave1"]
    assert cf.budget.spent_usd == pytest.approx(0.55)
