"""P3 — self-measurement: cost/recall calibration store and the $0 closed loop.

No API anywhere here: ``calibrate_free`` only ever drives the local, $0
detector floor (spellscan + LanguageTool — see tests/galley/test_local_adapters.py
for why those two are the ones safe to run in a unit test), and every other
test builds its case files by hand, the way tests/galley/test_casefile.py and
tests/galley/test_brain.py do.
"""

from __future__ import annotations

import json

from galley.brain import make_planner
from galley.contracts import RULINGS
from galley.calibration import (
    Calibration,
    CostEntry,
    RecallRecord,
    calibrate_free,
    est_usd_per_kword,
    latest_recall,
    read_calibration,
    record_recall,
    record_run,
)
from galley.casefile import BudgetLedger, CaseFile
from galley.contracts import Hypothesis, WaveRecord
from galley.governor import Caps, Governor
from galley.seeding import RecallEstimate, is_seeded

from .fakes import make_manuscript


def _gov(total=100.0, max_waves=6):
    gov = Governor(BudgetLedger(),
                   Caps(total_usd=total, per_wave_usd=total,
                        max_waves=max_waves, max_panel_calls=8))
    gov.open_wave()
    return gov


def _scope_action(*, adapter, chapters=(), model="", cost_usd=0.0,
                  findings_added=0, error_groups=()):
    return {
        "adapter": adapter,
        "scope": {
            "chapters": list(chapters),
            "para_ids": [],
            "error_groups": list(error_groups),
            "model": model,
            "passes": 1,
        },
        "cost_usd": cost_usd,
        "findings_added": findings_added,
        "coverage_notes": [],
    }


# --- record_run --------------------------------------------------------------


def test_record_run_extracts_cost_and_kwords(tmp_path):
    # Two chapters of 100 words each ("word " * 100 tokenizes to 100 words).
    ms = make_manuscript(*["word " * 100] * 2, chapter_size=1)
    path = tmp_path / "calibration.json"

    cf = CaseFile(book="b")
    cf.waves.append(WaveRecord(
        index=1,
        actions=(
            _scope_action(adapter="single_pass", chapters=(0,),
                         model="claude-x", cost_usd=1.0),
        ),
        spend_usd=1.0,
        findings_added=0,
    ))

    calibration = record_run(cf, ms, path, now="2026-01-01T00:00:00Z")

    entry = calibration.cost["single_pass:claude-x"]
    assert entry.adapter == "single_pass"
    assert entry.model == "claude-x"
    assert entry.kwords_total == 0.1  # 100 words = 0.1 kword
    assert entry.cost_usd_total == 1.0
    assert entry.samples == 1
    assert entry.usd_per_kword == 10.0  # $1.00 / 0.1 kword
    assert entry.updated_at == "2026-01-01T00:00:00Z"

    # The write is durable: reading back from disk yields the same entry.
    reread = read_calibration(path)
    assert reread.cost["single_pass:claude-x"].usd_per_kword == 10.0


def test_record_run_accumulates_across_calls(tmp_path):
    ms = make_manuscript(*["word " * 100] * 2, chapter_size=1)
    path = tmp_path / "calibration.json"

    cf1 = CaseFile(book="b")
    cf1.waves.append(WaveRecord(index=1, actions=(
        _scope_action(adapter="single_pass", chapters=(0,), model="m",
                     cost_usd=1.0),
    )))
    record_run(cf1, ms, path, now="2026-01-01T00:00:00Z")

    cf2 = CaseFile(book="b")
    cf2.waves.append(WaveRecord(index=1, actions=(
        _scope_action(adapter="single_pass", chapters=(1,), model="m",
                     cost_usd=1.0),
    )))
    calibration = record_run(cf2, ms, path, now="2026-01-02T00:00:00Z")

    entry = calibration.cost["single_pass:m"]
    assert entry.samples == 2
    assert entry.cost_usd_total == 2.0
    assert entry.kwords_total == 0.2
    assert entry.usd_per_kword == 10.0
    assert entry.updated_at == "2026-01-02T00:00:00Z"


def test_record_run_skips_costless_or_scopeless_actions(tmp_path):
    ms = make_manuscript(*["word " * 100] * 2, chapter_size=1)
    path = tmp_path / "calibration.json"

    cf = CaseFile(book="b")
    cf.waves.append(WaveRecord(index=1, actions=(
        {"adapter": "single_pass", "skipped": "budget exhausted"},
        {"adapter": "single_pass", "scope": {"chapters": [9]}, "cost_usd": 3.0},
    )))
    calibration = record_run(cf, ms, path)
    # Neither action resolves to any scoped words (missing scope / unknown
    # chapter), so nothing is recorded.
    assert calibration.cost == {}


def test_record_run_free_adapter_records_zero_rate(tmp_path):
    ms = make_manuscript(*["word " * 50] * 1)
    path = tmp_path / "calibration.json"
    cf = CaseFile(book="b")
    cf.waves.append(WaveRecord(index=1, actions=(
        _scope_action(adapter="spellscan", cost_usd=0.0),
    )))
    calibration = record_run(cf, ms, path)
    entry = calibration.cost["spellscan:(default)"]
    assert entry.kwords_total == 0.05
    assert entry.usd_per_kword == 0.0


def test_record_run_excludes_zero_cost_paid_passes(tmp_path):
    # A paid adapter that billed $0 (subagent mode, a failed call) is not a
    # free pair — folding its words in would price the adapter toward zero.
    ms = make_manuscript(*["word " * 100] * 2, chapter_size=1)
    path = tmp_path / "calibration.json"
    cf = CaseFile(book="b")
    cf.waves.append(WaveRecord(index=1, actions=(
        _scope_action(adapter="single_pass", chapters=(0,), model="m", cost_usd=0.0),
        _scope_action(adapter="single_pass", chapters=(1,), model="m", cost_usd=1.0),
        _scope_action(adapter="languagetool", chapters=(0,), cost_usd=0.0),
    )))
    calibration = record_run(cf, ms, path)
    paid = calibration.cost["single_pass:m"]
    assert paid.kwords_total == 0.1 and paid.cost_usd_total == 1.0
    assert calibration.cost["languagetool:(default)"].kwords_total == 0.1  # free stays free


def test_record_run_accepts_a_synthesized_casefile(tmp_path):
    import json

    from galley.casefile_synth import casefile_from_run

    run = tmp_path / "run"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps({
        "source": "Book.docx", "model": "m", "cost": {"total_usd": 0.5},
        "findings": [{"finding_id": "f-1", "para_id": "body-0001",
                      "error_type": "spelling", "original_text": "teh",
                      "corrected_text": "the", "status": "validated",
                      "anchor": {"start": 0, "end": 3}}]}), encoding="utf-8")
    cf = casefile_from_run(run)
    assert {v.ruling for v in cf.verdicts} <= set(RULINGS)
    ms = make_manuscript(*["word " * 100] * 2)
    calibration = record_run(cf, ms, tmp_path / "calibration.json")
    entry = calibration.cost["review:m"]
    assert entry.cost_usd_total == 0.5 and entry.kwords_total == 0.2


# --- est_usd_per_kword ---------------------------------------------------


def test_est_usd_per_kword_exact_and_fallback_and_default():
    calibration = Calibration(cost={
        "single_pass:claude-a": CostEntry(
            "single_pass", "claude-a", cost_usd_total=2.0, kwords_total=1.0,
            samples=1, updated_at="t"),
        "single_pass:claude-b": CostEntry(
            "single_pass", "claude-b", cost_usd_total=1.0, kwords_total=2.0,
            samples=1, updated_at="t"),
    })

    # Exact match.
    assert est_usd_per_kword(calibration, "single_pass", "claude-a", 0.10) == 2.0

    # No exact match for "claude-c": falls back to the adapter-wide weighted
    # average across every model recorded for it — (2.0 + 1.0) / (1.0 + 2.0).
    assert est_usd_per_kword(
        calibration, "single_pass", "claude-c", 0.10
    ) == 1.0

    # An adapter with nothing recorded at all falls back to the default.
    assert est_usd_per_kword(calibration, "docproof_ladder", "x", 0.10) == 0.10


# --- planner uses the calibrated rate ------------------------------------


def test_planner_uses_calibrated_rate_over_the_frozen_default():
    # One chapter, 1000 words -> 1.0 kword. Frozen default $0.10/kword prices
    # it at $0.10 (affordable against any budget); a calibrated $20/kword rate
    # prices it at $20, which a small budget cannot afford.
    ms = make_manuscript(*["word " * 1000] * 1, chapter_size=1)
    hyps = [Hypothesis(chapter=0, error_class="spelling", why="", span_hint="")]

    calibration = Calibration(cost={
        "single_pass:m": CostEntry(
            "single_pass", "m", cost_usd_total=20.0, kwords_total=1.0,
            samples=3, updated_at="t"),
    })

    uncalibrated = make_planner(ms, model="m", est_usd_per_kword=0.10)
    assert len(uncalibrated(hyps, _gov(total=5.0), CaseFile(book="b"))) == 1

    calibrated = make_planner(ms, model="m", est_usd_per_kword=0.10,
                              calibration=calibration)
    assert calibrated(hyps, _gov(total=5.0), CaseFile(book="b")) == []
    # A big-enough budget still admits it at the calibrated rate.
    assert len(calibrated(hyps, _gov(total=100.0), CaseFile(book="b"))) == 1


def test_planner_default_behavior_unchanged_without_calibration():
    # calibration=None (the default) must reproduce the exact frozen-default
    # planner from tests/galley/test_brain.py.
    ms = make_manuscript(*["word " * 100] * 4, chapter_size=2)
    planner = make_planner(ms, est_usd_per_kword=0.10)
    hyps = [
        Hypothesis(chapter=0, error_class="comma_splice", why="", span_hint=""),
        Hypothesis(chapter=1, error_class="spelling", why="", span_hint=""),
    ]
    dispatches = planner(hyps, _gov(), CaseFile(book="b"))
    assert len(dispatches) == 2


# --- record_recall / latest_recall ---------------------------------------


def test_record_recall_and_latest_recall_roundtrip(tmp_path):
    path = tmp_path / "calibration.json"
    est1 = RecallEstimate(planted=8, caught=4, rate=0.5,
                          by_type={"missing_comma": (2, 4)}, caveat="c1")
    est2 = RecallEstimate(planted=8, caught=6, rate=0.75,
                          by_type={"missing_comma": (3, 4)}, caveat="c2")

    record_recall(est1, path, now="2026-01-01T00:00:00Z", book="Book A")
    record_recall(est2, path, now="2026-01-02T00:00:00Z", book="Book A")

    calibration = read_calibration(path)
    assert len(calibration.recall) == 2
    assert calibration.recall[0].rate == 0.5
    assert calibration.recall[1].rate == 0.75

    latest = latest_recall(path)
    assert isinstance(latest, RecallEstimate)
    assert latest.planted == 8
    assert latest.caught == 6
    assert latest.rate == 0.75
    assert latest.by_type == {"missing_comma": (3, 4)}


def test_latest_recall_none_when_no_history(tmp_path):
    path = tmp_path / "calibration.json"
    assert latest_recall(path) is None


def test_latest_recall_is_keyed_by_book(tmp_path):
    path = tmp_path / "calibration.json"
    record_recall(RecallEstimate(planted=4, caught=1, rate=0.25), path,
                  now="t1", book="Book A")
    record_recall(RecallEstimate(planted=4, caught=3, rate=0.75), path,
                  now="t2", book="Book B")
    # Backward-compatible form: the last record of any book, tagged with it.
    latest = latest_recall(path)
    assert isinstance(latest, RecallEstimate)
    assert latest.rate == 0.75 and latest.book == "Book B"
    # Keyed: Book A's own gauge, not Book B's newer one.
    assert latest_recall(path, book="Book A").rate == 0.25
    assert latest_recall(path, book="Book A").book == "Book A"
    # A book never gauged has no recall, rather than someone else's.
    assert latest_recall(path, book="Book C") is None


# --- Calibration to_json/from_json round-trip -----------------------------


def test_calibration_json_roundtrip():
    calibration = Calibration(
        cost={"spellscan:(default)": CostEntry("spellscan", "", 0.0, 1.5, 2, "t")},
        recall=[RecallRecord(planted=2, caught=1, rate=0.5,
                             by_type={"homophone": (1, 2)}, caveat="x",
                             book="b", recorded_at="t")],
    )
    text = json.dumps(calibration.to_json())
    revived = Calibration.from_json(json.loads(text))
    assert revived.to_json() == calibration.to_json()


# --- the $0 closed loop: calibrate_free -----------------------------------


def _fixture_manuscript():
    # Plain, unremarkable prose across a few paragraphs/chapters so seed_copy
    # has real mutation sites (commas, capitalized mid-sentence words, etc.).
    paras = [
        "The old house stood on the hill, quiet and still, for many years.",
        "Its owner had left long ago, and the town forgot her name.",
        "Every morning, the wind came down from the North and rattled the door.",
        "No one dared to open it, not even the boldest of the local children.",
    ]
    return make_manuscript(*paras, chapter_size=2)


def test_calibrate_free_seed_review_score_loop():
    ms = _fixture_manuscript()
    result = calibrate_free(ms, 3, rng_seed=0, book="Fixture Book",
                            now="2026-01-01T00:00:00Z")

    # The original manuscript is untouched; only the returned copy is seeded.
    assert not is_seeded(ms)
    assert is_seeded(result.seeded)

    assert isinstance(result.estimate, RecallEstimate)
    assert result.estimate.planted <= 3
    assert 0.0 <= result.estimate.rate <= 1.0

    # A synthetic single wave, $0 throughout, ran the free detector floor.
    assert len(result.casefile.waves) == 1
    wave = result.casefile.waves[0]
    assert wave.spend_usd == 0.0
    adapters_run = {a["adapter"] for a in wave.actions}
    assert adapters_run == {"spellscan", "languagetool"}
    for action in wave.actions:
        assert action["cost_usd"] == 0.0
    assert wave.started_at == "2026-01-01T00:00:00Z"


def test_calibrate_free_feeds_record_run_and_record_recall(tmp_path):
    ms = _fixture_manuscript()
    result = calibrate_free(ms, 3, rng_seed=0, book="Fixture Book")
    path = tmp_path / "calibration.json"

    calibration = record_run(result.casefile, result.seeded, path)
    # Both free adapters ran over real (nonzero) word counts, at $0/kword.
    for key in ("spellscan:(default)", "languagetool:(default)"):
        entry = calibration.cost.get(key)
        assert entry is not None
        assert entry.kwords_total > 0
        assert entry.usd_per_kword == 0.0

    record_recall(result.estimate, path, book="Fixture Book")
    latest = latest_recall(path)
    assert latest.planted == result.estimate.planted
    assert latest.caught == result.estimate.caught


def test_calibrate_free_is_deterministic():
    ms = _fixture_manuscript()
    r1 = calibrate_free(ms, 3, rng_seed=7, now="t")
    r2 = calibrate_free(ms, 3, rng_seed=7, now="t")
    assert r1.answer_key.to_json() == r2.answer_key.to_json()
    assert r1.estimate == r2.estimate
