"""D1 — governor: budget allocator that never spends past its caps.

Core property: under ANY sequence of ``can_spend``-gated charges, total spend
never exceeds ``total_usd`` and per-wave spend never exceeds ``per_wave_usd``.
"""

import random

import pytest

from galley.casefile import BudgetLedger
from galley.governor import (
    PANEL_LABEL_PREFIX,
    BudgetError,
    Caps,
    Governor,
    PanelLimitError,
    WaveLimitError,
)


def _gov(
    total_usd: float = 10.0,
    per_wave_usd: float = 3.0,
    max_waves: int = 5,
    max_panel_calls: int = 2,
) -> Governor:
    caps = Caps(total_usd, per_wave_usd, max_waves, max_panel_calls)
    return Governor(BudgetLedger(), caps)


# ---- core property: spend never exceeds caps ---------------------------


def test_gated_charges_never_exceed_caps_randomized():
    rng = random.Random(1234)
    for _ in range(200):
        caps = Caps(
            total_usd=rng.uniform(1.0, 20.0),
            per_wave_usd=rng.uniform(0.2, 5.0),
            max_waves=rng.randint(1, 8),
            max_panel_calls=rng.randint(0, 4),
        )
        gov = Governor(BudgetLedger(), caps)
        gov.open_wave()
        for _step in range(300):
            # Sometimes cross into a new wave; open_wave resets the per-wave floor.
            if rng.random() < 0.05 and gov.can_open_wave():
                gov.open_wave()
            cost = rng.uniform(0.0, 2.0)
            if gov.can_spend(cost):
                gov.charge(cost, "step")
            # Invariants must hold after every attempted action.
            assert gov.spent_usd <= caps.total_usd
            assert gov.wave_spent_usd <= caps.per_wave_usd
        assert gov.remaining_usd >= 0.0


def test_charge_over_cap_raises_and_can_spend_false():
    gov = _gov(total_usd=10.0, per_wave_usd=1.0)
    gov.open_wave()
    gov.charge(0.8, "ok")
    # 0.8 + 0.3 = 1.1 > per_wave 1.0
    assert gov.can_spend(0.3) is False
    with pytest.raises(BudgetError):
        gov.charge(0.3, "too much for the wave")
    # nothing recorded past the guard
    assert gov.spent_usd == pytest.approx(0.8)


def test_can_spend_is_exact_predicate_charge_enforces():
    gov = _gov(total_usd=1.0, per_wave_usd=1.0)
    gov.open_wave()
    # Exactly filling the cap is allowed.
    assert gov.can_spend(1.0) is True
    gov.charge(1.0, "fill")
    assert gov.spent_usd == pytest.approx(1.0)
    # Now nothing more, not even a penny.
    assert gov.can_spend(0.01) is False
    assert gov.can_spend(0.0) is True  # a free action is always spendable


def test_negative_cost_is_never_spendable():
    gov = _gov()
    gov.open_wave()
    assert gov.can_spend(-1.0) is False
    with pytest.raises(BudgetError):
        gov.charge(-1.0, "refund not a thing")


def test_total_cap_binds_across_waves():
    gov = _gov(total_usd=2.5, per_wave_usd=2.0, max_waves=5)
    gov.open_wave()
    gov.charge(2.0, "w1")
    gov.open_wave()  # per-wave counter resets to 0
    assert gov.wave_spent_usd == 0.0
    # per-wave would allow 2.0, but total only has 0.5 left
    assert gov.can_spend(2.0) is False
    assert gov.can_spend(0.5) is True
    gov.charge(0.5, "w2")
    assert gov.spent_usd == pytest.approx(2.5)
    assert gov.remaining_usd == pytest.approx(0.0)


# ---- ledger totals -----------------------------------------------------


def test_ledger_total_equals_sum_of_charges():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0)
    gov.open_wave()
    costs = [0.10, 1.25, 0.03, 2.50]
    for c in costs:
        gov.charge(c, "x")
    assert gov.ledger.spent_usd == pytest.approx(sum(costs))
    assert gov.spent_usd == pytest.approx(sum(c.cost_usd for c in gov.ledger.charges))


# ---- wave accounting ---------------------------------------------------


def test_per_wave_cap_resets_on_new_wave():
    gov = _gov(total_usd=100.0, per_wave_usd=1.0, max_waves=3)
    gov.open_wave()
    gov.charge(1.0, "wave1 full")
    assert gov.can_spend(0.01) is False
    gov.open_wave()
    assert gov.wave_spent_usd == 0.0
    assert gov.can_spend(1.0) is True


def test_max_waves_enforced():
    gov = _gov(max_waves=2)
    gov.open_wave()
    gov.open_wave()
    assert gov.can_open_wave() is False
    with pytest.raises(WaveLimitError):
        gov.open_wave()


def test_wave_numbers_and_counters():
    gov = _gov(max_waves=4)
    assert gov.current_wave == 0
    assert gov.open_wave() == 1
    assert gov.open_wave() == 2
    assert gov.waves_opened == 2
    assert gov.waves_remaining == 2


# ---- panel-call accounting ---------------------------------------------


def test_max_panel_calls_enforced():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0, max_panel_calls=2)
    gov.open_wave()
    gov.charge_panel_call(0.5, "escalate a")
    gov.charge_panel_call(0.5, "escalate b")
    assert gov.can_escalate() is False
    with pytest.raises(PanelLimitError):
        gov.charge_panel_call(0.5, "escalate c")
    assert gov.panel_calls == 2
    assert gov.panel_calls_remaining == 0


def test_panel_call_respects_budget_and_leaves_no_side_effect():
    gov = _gov(total_usd=0.4, per_wave_usd=0.4, max_panel_calls=3)
    gov.open_wave()
    with pytest.raises(BudgetError):
        gov.charge_panel_call(0.5, "too expensive")
    # counter not bumped, nothing charged
    assert gov.panel_calls == 0
    assert gov.spent_usd == 0.0


def test_panel_calls_recorded_in_ledger():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0, max_panel_calls=2)
    gov.open_wave()
    gov.charge_panel_call(0.25, "escalate")
    labels = [c.label for c in gov.ledger.charges]
    assert any(lbl.startswith(PANEL_LABEL_PREFIX) for lbl in labels)


# ---- stop rule ---------------------------------------------------------


def test_should_stop_fires_exactly_at_threshold():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0, max_waves=9)
    gov.open_wave()
    threshold = 0.50
    assert gov.should_stop(0.49, threshold) is False
    assert gov.should_stop(0.50, threshold) is True  # exactly at threshold
    assert gov.should_stop(0.51, threshold) is True


def test_should_stop_when_waves_exhausted():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0, max_waves=1)
    gov.open_wave()
    # marginal well under threshold, but no wave left to open
    assert gov.can_open_wave() is False
    assert gov.should_stop(0.0, 1.0) is True


def test_should_stop_when_budget_floor_reached():
    gov = _gov(total_usd=1.0, per_wave_usd=1.0, max_waves=9)
    gov.open_wave()
    gov.charge(1.0, "spent it all")
    assert gov.remaining_usd == pytest.approx(0.0)
    assert gov.should_stop(0.0, 1.0) is True


def test_should_stop_false_when_all_clear():
    gov = _gov(total_usd=100.0, per_wave_usd=100.0, max_waves=9)
    gov.open_wave()
    assert gov.should_stop(0.0, 1.0) is False


# ---- caps round-trip & reconstruction ----------------------------------


def test_caps_snapshot_stashed_in_ledger():
    caps = Caps(10.0, 3.0, 5, 2)
    gov = Governor(BudgetLedger(), caps)
    assert gov.ledger.caps == caps.to_json()


def test_caps_round_trip_through_ledger():
    caps = Caps(12.5, 4.25, 7, 3)
    gov = Governor(BudgetLedger(), caps)
    gov.open_wave()
    gov.charge(1.0, "a")
    gov.open_wave()
    gov.charge(0.5, "b")
    gov.charge_panel_call(0.25, "escalate")

    # Serialize the ledger and rebuild the governor from it.
    ledger2 = BudgetLedger.from_json(gov.ledger.to_json())
    gov2 = Governor.from_ledger(ledger2)

    assert gov2.caps == caps
    assert gov2.spent_usd == pytest.approx(gov.spent_usd)
    # counters recovered from the charges
    assert gov2.current_wave == gov.current_wave
    assert gov2.waves_opened == gov.waves_opened
    assert gov2.wave_spent_usd == pytest.approx(gov.wave_spent_usd)
    assert gov2.panel_calls == gov.panel_calls


def test_reconstructed_governor_still_enforces_caps():
    caps = Caps(total_usd=2.0, per_wave_usd=2.0, max_waves=2, max_panel_calls=1)
    gov = Governor(BudgetLedger(), caps)
    gov.open_wave()
    gov.charge(1.5, "a")

    ledger2 = BudgetLedger.from_json(gov.ledger.to_json())
    gov2 = Governor.from_ledger(ledger2)
    # total has 0.5 left; per-wave also 0.5 left
    assert gov2.can_spend(0.5) is True
    assert gov2.can_spend(0.6) is False
    # already opened one wave; one more allowed then exhausted
    assert gov2.can_open_wave() is True
    gov2.open_wave()
    assert gov2.can_open_wave() is False


def test_caps_from_json_drops_unknown_keys():
    caps = Caps.from_json(
        {"total_usd": 5.0, "per_wave_usd": 1.0, "max_waves": 3,
         "max_panel_calls": 1, "surprise": "ignored"}
    )
    assert caps == Caps(5.0, 1.0, 3, 1)


# ---- allow_over_cap: money already spent is recorded, and flagged ---------

def test_allow_over_cap_records_the_charge_and_flags_it():
    gov = _gov(total_usd=1.0, per_wave_usd=1.0)
    gov.open_wave()
    gov.charge(0.8, "ok")
    # A detector billed 0.5 after the fact: past the cap, but the money is gone.
    entry = gov.charge(0.5, "wave1:ladder", allow_over_cap=True)
    assert gov.spent_usd == pytest.approx(1.3)       # the ledger is the truth
    assert gov.overruns == (entry,)
    assert gov.overrun_usd == pytest.approx(0.3)
    # Ordinary gated charges are still refused.
    with pytest.raises(BudgetError):
        gov.charge(0.1, "gated")
    # A negative cost is never recorded, allow_over_cap or not.
    with pytest.raises(BudgetError):
        gov.charge(-0.1, "refund?", allow_over_cap=True)


def test_overruns_are_reconstructed_from_a_loaded_ledger():
    gov = _gov(total_usd=1.0, per_wave_usd=1.0)
    gov.open_wave()
    gov.charge(0.8, "ok")
    gov.charge(0.5, "over", allow_over_cap=True)
    ledger = BudgetLedger.from_json(gov.ledger.to_json())
    rebuilt = Governor.from_ledger(ledger)
    assert [c.label for c in rebuilt.overruns] == ["over"]
    assert rebuilt.overrun_usd == pytest.approx(0.3)
