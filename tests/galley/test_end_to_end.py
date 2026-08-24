"""I2 — end-to-end T2 dry run on fakes: ladder -> targeted wave -> adjudicate -> letter.

Offline, deterministic, well under a second. Asserts the budget is honored to the
cent, the case file is complete and resumable, the letter mentions every wave, and
every artifact lands inside the run directory.
"""

from pathlib import Path

import pytest

from galley.adjudicate import adjudicate
from galley.adapters import Scope
from galley.casefile import CaseFile
from galley.contracts import Hypothesis
from galley.letter import render_letter, render_style_sheet
from galley.orchestrator import Dispatch, run_galley

from tests.galley.fakes import FakeDetector, gfinding, make_manuscript

FIXED = lambda: "2026-08-21T00:00:00Z"  # noqa: E731


def _adapters():
    ladder = FakeDetector(
        name="docproof_ladder",
        scripted=[
            gfinding("g-1", "body-0001", "teh", "the", wave=1),
            gfinding("g-2", "body-0002", "recieve", "receive", wave=1),
        ],
        cost_usd=0.50,
    )
    reread = FakeDetector(
        name="reread",
        scripted=[
            gfinding("g-3", "body-0003", "occured", "occurred", wave=2),
            gfinding("g-1", "body-0001", "teh", "the", wave=2),  # dup id, unioned out
        ],
        cost_usd=0.25,
    )
    return {"docproof_ladder": ladder, "reread": reread}, ladder


def _audit(cf, ms):
    return [Hypothesis(chapter=1, error_class="typo", why="dense")] if len(cf.waves) < 2 else []


def _plan(hyps, gov, cf):
    return [Dispatch("reread", Scope())] if len(cf.waves) < 2 else []


def test_t2_end_to_end_dry_run(tmp_path):
    ms = make_manuscript(
        "Chapter one has teh typo.",
        "The second paragraph will recieve a fix.",
        "In chapter two an error occured here.",
        "A clean closing paragraph.",
        chapter_size=2,
    )
    out = tmp_path / "run"
    adapters, ladder = _adapters()

    cf = run_galley(
        ms, "T2", budget_usd=10.0, out_dir=out,
        adapters=adapters, audit=_audit, plan_wave=_plan, clock=FIXED, book="Fixture",
    )

    # Budget honored to the cent: 0.50 (ladder) + 0.25 (reread).
    assert cf.budget.spent_usd == pytest.approx(0.75)
    assert [w.index for w in cf.waves] == [1, 2]
    assert {f.id for f in cf.findings} == {"g-1", "g-2", "g-3"}  # union, no dup

    # Adjudicate the union (arbitration only, offline) and record verdicts.
    result = adjudicate(cf.findings)
    cf.verdicts.extend(result.verdicts)
    assert len(result.kept) == 3  # disjoint spans, all survive

    # The letter mentions every wave and the total spend.
    letter = render_letter(cf, out, ms=ms)
    text = letter.read_text(encoding="utf-8")
    for wave in cf.waves:
        assert str(wave.index) in text
    assert "0.75" in text
    style = render_style_sheet(cf, out)
    assert style.is_file()

    # The case file is complete and resumable: reload, re-run, nothing changes and
    # the ladder is not re-run.
    reloaded = CaseFile.load(out / "casefile.json")
    assert [w.index for w in reloaded.waves] == [1, 2]

    cf2 = run_galley(
        ms, "T2", budget_usd=10.0, out_dir=out,
        adapters=adapters, audit=_audit, plan_wave=_plan, clock=FIXED, book="Fixture",
    )
    assert [w.index for w in cf2.waves] == [1, 2]
    assert len(ladder.calls) == 1  # wave 1 never re-ran on resume

    # Every artifact is inside the run directory (nothing written elsewhere).
    produced = {p.name for p in Path(out).iterdir()}
    assert {"casefile.json", "letter.md", "style-sheet.md"} <= produced
    for p in Path(out).rglob("*"):
        assert Path(out) in p.parents or p.parent == Path(out)
