"""E4 — mid-run notify: one digest per wave, an alarm for each trigger class.

No email is sent from these tests: a FakeTransport captures everything.
"""

from galley.casefile import BudgetLedger
from galley.contracts import Hypothesis, WaveRecord
from galley.governor import Caps, Governor
from galley.orchestrator import (
    ALARM_BUDGET_CAP,
    ALARM_DEGRADED,
    ALARM_TRUNCATION,
    ALARM_ZERO_COST,
    FREE_ADAPTERS,
    Dispatch,
    _wave_alarms,
    make_notifier,
    run_galley,
)
from galley.adapters import Scope

from tests.galley.fakes import FakeDetector, gfinding, make_manuscript

FIXED = lambda: "2026-08-21T00:00:00Z"  # noqa: E731


class FakeTransport:
    """Captures (kind, subject, body); never touches the network."""

    def __init__(self):
        self.sent = []

    def send(self, kind, subject, body):
        self.sent.append((kind, subject, body))

    def of(self, kind):
        return [s for s in self.sent if s[0] == kind]


def _gov(total=100.0):
    return Governor(BudgetLedger(), Caps(total, total, 6, 8))


def _rec(actions, index=1):
    return WaveRecord(index=index, actions=tuple(actions))


# ---- alarm detection ----------------------------------------------------

def test_degraded_pass_alarms():
    rec = _rec([{"adapter": "docproof_ladder", "cost_usd": 0.5,
                 "coverage_notes": ["degraded: smoothing: dead model"]}])
    alarms = _wave_alarms(rec, _gov(), streak={"n": 0}, free_adapters=FREE_ADAPTERS)
    assert any(a["alarm"] == ALARM_DEGRADED for a in alarms)


def test_zero_cost_paid_detector_alarms_but_free_one_does_not():
    rec = _rec([
        {"adapter": "sapling", "cost_usd": 0.0, "findings_added": 0, "coverage_notes": []},
        {"adapter": "spellscan", "cost_usd": 0.0, "findings_added": 1, "coverage_notes": []},
    ])
    alarms = _wave_alarms(rec, _gov(), streak={"n": 0}, free_adapters=FREE_ADAPTERS)
    zero = [a for a in alarms if a["alarm"] == ALARM_ZERO_COST]
    assert len(zero) == 1 and zero[0]["detail"] == "sapling"


def test_truncation_streak_alarms_only_on_the_third_wave():
    streak = {"n": 0}
    gov = _gov()
    fired = []
    for i in (1, 2, 3):
        rec = _rec([{"adapter": "x", "cost_usd": 0.1,
                     "coverage_notes": [f"unruled: pass lost 2 windows (wave {i})"]}], index=i)
        alarms = _wave_alarms(rec, gov, streak=streak, free_adapters=FREE_ADAPTERS)
        fired.append(any(a["alarm"] == ALARM_TRUNCATION for a in alarms))
    assert fired == [False, False, True]


def test_truncation_streak_resets_on_a_clean_wave():
    streak = {"n": 0}
    gov = _gov()
    # two truncated, one clean, then it takes three more to alarm again
    seq = ["unruled", "unruled", "clean", "unruled", "unruled", "unruled"]
    fired = []
    for i, kind in enumerate(seq, start=1):
        notes = [] if kind == "clean" else ["unruled: lost a window"]
        rec = _rec([{"adapter": "x", "cost_usd": 0.1, "coverage_notes": notes}], index=i)
        alarms = _wave_alarms(rec, gov, streak=streak, free_adapters=FREE_ADAPTERS)
        fired.append(any(a["alarm"] == ALARM_TRUNCATION for a in alarms))
    assert fired == [False, False, False, False, False, True]


def test_budget_cap_alarms_on_skipped_dispatch():
    rec = _rec([{"adapter": "reread", "skipped": "over budget", "cost_usd": 0.6}])
    alarms = _wave_alarms(rec, _gov(), streak={"n": 0}, free_adapters=FREE_ADAPTERS)
    assert any(a["alarm"] == ALARM_BUDGET_CAP for a in alarms)


# ---- notifier + transport ----------------------------------------------

def test_one_digest_per_wave_and_no_email():
    transport = FakeTransport()
    notify = make_notifier(transport, book="Fixture")
    ms = make_manuscript("a", "b", "c")
    ladder = FakeDetector(name="docproof_ladder",
                          scripted=[gfinding("g-1", "body-0001", "teh", "the")],
                          cost_usd=0.5)
    reread = FakeDetector(name="reread",
                          scripted=[gfinding("g-2", "body-0002", "x", "y")],
                          cost_usd=0.2)

    def plan(hyps, gov, cf):
        return [Dispatch("reread", Scope())] if len(cf.waves) < 2 else []

    import tempfile
    with tempfile.TemporaryDirectory() as out:
        run_galley(ms, "T2", 10.0, out,
                   adapters={"docproof_ladder": ladder, "reread": reread},
                   plan_wave=plan, notify=notify, clock=FIXED, book="Fixture")

    # Two waves -> two digests. Subjects carry the book tag and the spend.
    digests = transport.of("wave")
    assert len(digests) == 2
    assert all("[Fixture]" in s[1] for s in digests)


def test_alarm_flows_through_the_notifier():
    transport = FakeTransport()
    notify = make_notifier(transport)
    ms = make_manuscript("a", "b", "c")
    ladder = FakeDetector(name="docproof_ladder",
                          scripted=[gfinding("g-1", "body-0001", "teh", "the")],
                          cost_usd=0.5)
    # A paid detector that bills nothing -> a zero-cost alarm mid-run.
    sapling = FakeDetector(name="sapling", scripted=[], cost_usd=0.0)

    def plan(hyps, gov, cf):
        return [Dispatch("sapling", Scope())] if len(cf.waves) < 2 else []

    import tempfile
    with tempfile.TemporaryDirectory() as out:
        run_galley(ms, "T2", 10.0, out,
                   adapters={"docproof_ladder": ladder, "sapling": sapling},
                   plan_wave=plan, notify=notify, clock=FIXED)

    alarms = transport.of("alarm")
    assert any("zero_cost_detector" in s[1] for s in alarms)
