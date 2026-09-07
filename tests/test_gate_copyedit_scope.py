"""The plan gate: what a run WILL DO beats what the plan says about it."""
from __future__ import annotations

import textwrap

from galley.driver import (config_copyedit_lanes, gate_decision, read_plan)


def _plan(tmp_path, body: str):
    p = tmp_path / "PLAN.md"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return read_plan(p)


def _config(tmp_path, body: str):
    p = tmp_path / "mech.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_mechanical_rotated_reread_is_not_a_copyedit_line(tmp_path):
    """The line that refused every book on 2026-09-06: verify's rotated
    two-pass reread is mechanical, not the tabled copy-edit reread phase."""
    plan = _plan(tmp_path, """
        1. sweeps  $0.00
        7. verify (rotated reread, 2 passes/window) + settle + certify  $0.10
        TOTAL $1.15
        """)
    assert plan.copyedit_lines == []
    assert gate_decision(plan, 10.0)[0] is True


def test_a_bare_reread_line_is_still_refused(tmp_path):
    """With no mechanical phase named, a re-read line is the copy-edit phase."""
    plan = _plan(tmp_path, """
        4. wave-2 re-read of the opening chapters  $2.00
        TOTAL $2.00
        """)
    assert plan.copyedit_lines
    approved, reason = gate_decision(plan, 10.0)
    assert approved is False
    assert "copy-edit lane in scope" in reason


def test_flights_are_refused_even_beside_mechanical_words(tmp_path):
    """Only a bare re-read claim is ambiguous. 'flights' next to 'settle' is
    still the flight deck, and a nearby mechanical word must not excuse it."""
    plan = _plan(tmp_path, """
        5. flights (6 lenses) then settle  $3.00
        TOTAL $3.00
        """)
    assert plan.copyedit_lines
    assert gate_decision(plan, 10.0)[0] is False


def test_config_refuses_a_lane_the_prose_never_mentions(tmp_path):
    """The structural half: a plan that reads clean but a config that opens
    the rewrite lane is refused on the config."""
    plan = _plan(tmp_path, """
        1. mechanical ladder  $1.00
        TOTAL $1.00
        """)
    cfg = _config(tmp_path, """
        smoothing:
          enabled: true
          edits: true
        rewrite:
          enabled: false
        """)
    assert config_copyedit_lanes(cfg) == ["smoothing.enabled", "smoothing.edits"]
    approved, reason = gate_decision(plan, 10.0, config_path=cfg)
    assert approved is False
    assert "run config opens" in reason
    assert "smoothing.enabled" in reason


def test_a_locked_down_config_approves(tmp_path):
    plan = _plan(tmp_path, """
        1. mechanical ladder  $1.00
        TOTAL $1.00
        """)
    cfg = _config(tmp_path, """
        smoothing:
          enabled: false
          edits: false
        rewrite:
          enabled: false
        """)
    assert config_copyedit_lanes(cfg) == []
    assert gate_decision(plan, 10.0, config_path=cfg)[0] is True


def test_missing_config_is_not_fatal(tmp_path):
    """A config is not written until the plan is drafted; absence falls back
    to the prose scan rather than refusing."""
    assert config_copyedit_lanes(tmp_path / "nope.yaml") == []


def test_budget_still_outranks_everything(tmp_path):
    plan = _plan(tmp_path, """
        1. mechanical ladder  $99.00
        TOTAL $99.00
        """)
    approved, reason = gate_decision(plan, 10.0)
    assert approved is False
    assert "over the $10.00 budget" in reason


def test_stage_locks_line_is_not_a_copyedit_line(tmp_path):
    """The line that refused Test - Book One on 2026-09-07: PLAN.md reported
    that the mechanical-wave stage LOCKS smoothing off, and the gate read the
    word 'smoothing' as scope. Only 'locked' was excused, never 'locks'."""
    plan = _plan(tmp_path, """
        1. sweeps  $0.00
        Config: runs/mech.yaml (genre literary_memoir + stage
        mechanical-wave; stage locks won on `smoothing.enabled`).
        TOTAL $1.15
        """)
    assert plan.copyedit_lines == []
    assert gate_decision(plan, 10.0)[0] is True


def test_every_inflection_of_lock_excuses_the_line(tmp_path):
    from galley.driver import _is_copyedit_line

    for verb in ("lock", "locks", "locked", "locking"):
        assert not _is_copyedit_line(f"stage {verb} smoothing.enabled"), verb
    # "unlock" is not "lock": a plan that reopens the lane is still refused.
    assert _is_copyedit_line("stage unlocks smoothing.enabled")


def test_config_still_beats_a_plan_that_says_locks(tmp_path):
    """Widening the prose escape must not weaken the structural half: a config
    that really opens smoothing is refused however the plan describes it."""
    plan = _plan(tmp_path, """
        1. mechanical ladder (stage locks smoothing.enabled)  $1.00
        TOTAL $1.00
        """)
    cfg = _config(tmp_path, """
        smoothing:
          enabled: true
        """)
    assert plan.copyedit_lines == []
    approved, reason = gate_decision(plan, 10.0, config_path=cfg)
    assert approved is False
    assert "smoothing.enabled" in reason


# ===========================================================================
# The gate scans the plan's promises — priced line items — not its prose
# ===========================================================================

def test_explaining_that_copyedit_lanes_are_shut_is_not_scope(tmp_path):
    """The four lines that refused The Lighthouse at Gull Point on 2026-09-07,
    verbatim, around a clean priced plan. Every one describes the mechanical-
    only doctrine; none is a line item."""
    plan = _plan(tmp_path, """
        ```
        0.  chapter sweep (Luna 6 windows + Sonnet 6 windows $0)   $0.35
        2.  mechanical ladder (Luna+Sonnet ensemble, Luna verifier) $0.91
        7.  verify (rotated reread, 2 passes) + settle + certify   $0
        TOTAL $2.00  ·  stop: $2/finding marginal, ONE wave
        ```

        Mechanical lanes and $0 lanes only. The copy-edit-scope lines are absent from
        this plan by go-live scope — not recommended against, simply not here.

        `--mechanical-only` records the copy-edit lanes as shut, so `certify` FAILS the
        delivery if any copy-edit finding, lane or artifact appears. One tracked-change

        3. **The book arguably wants copy-editing.** Recorded here and for the letter,
           and nowhere else. The damage is mechanical, and the mechanical pass
           resolves it. No copy-edit lane
        """)
    assert plan.copyedit_lines == [], plan.copyedit_lines
    assert plan.total_usd == 2.00
    assert gate_decision(plan, 5.0)[0] is True


def test_a_priced_copyedit_item_is_still_refused_in_any_list_style(tmp_path):
    """Scoping to line items must not open a hole: a priced promise of
    copy-edit work is refused whether numbered, lettered, bulleted or tabled."""
    for item in ("2a. Copy-edit flights, 6 lenses  $0.30",
                 "5) merge desk over the ladder output  $1.10",
                 "- wave-2 re-read of the opening chapters  $2.00",
                 "| 6 | smoothing pass, edits mode | $0.80 |"):
        plan = _plan(tmp_path, f"""
            1. mechanical ladder  $1.00
            {item}
            TOTAL $4.00
            """)
        assert plan.copyedit_lines == [item], item
        assert gate_decision(plan, 10.0)[0] is False


def test_an_unpriced_line_is_prose_even_with_a_marker(tmp_path):
    """A numbered caveat is discussion, not a promise. The config gate, not
    the prose gate, is what catches a lane that is really open."""
    from galley.driver import _is_plan_item

    assert not _is_plan_item("3. **The book arguably wants copy-editing.**")
    assert not _is_plan_item("Mechanical lanes and $0 lanes only.")
    assert _is_plan_item("2a. Copy-edit flights, 6 lenses  $0.30")
    assert _is_plan_item("0.  chapter sweep (Luna 6 windows $0)   $0.35")
