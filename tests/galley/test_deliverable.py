"""D5 — turning adjudicated GFindings into a real reviewed manuscript at $0.

Money-fenced throughout: `build_manuscript_deliverable` drives DocProof's own
`finish()` with every paid analyzer pass forced off and no provider
constructed at all — a stray API key in the test environment must never be
able to turn one of these into a real spend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docproof.config import load_config
from docproof.pipeline import prepare

from galley.adapters.docproof_ladder import DEFAULT_ERROR_DIR
from galley.adjudicate import AdjudicationResult
from galley.contracts import GFinding, Manuscript, Provenance, Span
from galley.deliverable import (
    build_manuscript_deliverable,
    gfinding_to_finding,
    partition_for_deliverable,
    zero_cost_config,
)
from galley.seeding import seed_copy

from tests.galley.fakes import gfinding, make_manuscript

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "config" / "default.yaml")
FIXTURE = ROOT / "tests" / "fixtures" / "tiny_novel.docx"

# Real content of the fixture book, established once (tests/galley/test_ladder_
# adapter.py and test_single_pass.py rely on the same planted typo): paragraph
# body-0028 reads "...opened teh door..." with "teh" at offset 10.
TEH_PARA = "body-0028"
TEH_START = 10


def _lean_cfg():
    cfg = load_config(CONFIG)
    cfg.audit = "off"  # don't let strict audit refuse to write the throwaway doc
    return cfg


def _fixture_manuscript() -> Manuscript:
    """The fixture book, ingested for real (not a placeholder) — the same
    document `build_manuscript_deliverable` will `prepare()` again itself, so
    a span recorded against this text anchors identically in both places."""
    prepared = prepare(_lean_cfg(), str(FIXTURE), DEFAULT_ERROR_DIR)
    paragraphs = {p.para_id: p.text for p in prepared.doc.paragraphs}
    order = tuple(p.para_id for p in prepared.doc.paragraphs)
    return Manuscript(paragraphs=paragraphs, order=order)


# ---------------------------------------------------------------------------
# zero_cost_config
# ---------------------------------------------------------------------------


def test_zero_cost_config_disables_every_paid_pass():
    cfg = load_config(CONFIG)
    # Turn everything paid ON, so the test proves zero_cost_config forces it
    # back off rather than merely preserving an already-off default.
    cfg.ensemble.detectors = [{"model": "gpt-5.6-luna"}]
    cfg.sapling.enabled = True
    cfg.repair.enabled = True
    cfg.smoothing.enabled = True
    cfg.chapter_continuity.enabled = True
    cfg.low_confidence.confirm = True
    cfg.meaning_check.enabled = True
    cfg.fix_check.enabled = True
    cfg.candidate_screening.mode = "apply"
    cfg.examination_graph.judgment.enabled = True
    cfg.languagetool.enabled = True

    z = zero_cost_config(cfg)
    assert z.ensemble.enabled is False
    assert z.sapling.enabled is False
    assert z.repair.enabled is False
    assert z.smoothing.enabled is False
    assert z.chapter_continuity.enabled is False
    assert z.low_confidence.confirm is False
    assert z.meaning_check.enabled is False
    assert z.fix_check.enabled is False
    assert z.candidate_screening.mode == "off"
    assert z.examination_graph.judgment.enabled is False
    assert z.languagetool.enabled is False
    # The caller's config is untouched — a deep copy, not a mutation in place.
    assert cfg.sapling.enabled is True
    assert cfg.ensemble.enabled is True


# ---------------------------------------------------------------------------
# _occurrence_at (via gfinding_to_finding, its only caller)
# ---------------------------------------------------------------------------


def test_gfinding_to_finding_anchors_at_the_recorded_offset():
    ms = make_manuscript("aba aba aba")  # "aba" occurs at 0, 4, 8
    g = gfinding("g-1", "body-0001", "aba", "ABA", start=4, end=7)
    f = gfinding_to_finding(g, ms)
    assert f is not None
    assert f.occurrence == 2  # the occurrence starting at offset 4


def test_gfinding_to_finding_returns_none_for_a_stale_span():
    ms = make_manuscript("nothing to see here")
    g = gfinding("g-1", "body-0001", "zzzznotreal", "zzz", start=0, end=11)
    assert gfinding_to_finding(g, ms) is None


def test_gfinding_to_finding_returns_none_for_an_unknown_paragraph():
    ms = make_manuscript("one paragraph only")
    g = gfinding("g-1", "body-9999", "one", "One", start=0, end=3)
    assert gfinding_to_finding(g, ms) is None


def test_gfinding_to_finding_query_sets_force_query():
    ms = make_manuscript("Kathryn walked in.")
    g = gfinding("g-1", "body-0001", "Kathryn", "", start=0, end=7,
                 note="possible continuity flag")
    f = gfinding_to_finding(g, ms, query=True)
    assert f is not None
    assert f.force_query is True
    assert f.explanation == "possible continuity flag"


# ---------------------------------------------------------------------------
# partition_for_deliverable
# ---------------------------------------------------------------------------


def test_partition_moves_self_declared_queries_out_of_kept():
    real_edit = gfinding("g-1", "body-0001", "teh", "the")
    self_query = gfinding("g-2", "body-0002", "Kathryn", "", confidence="query")
    routed_query = gfinding("g-3", "body-0003", "x", "y")
    result = AdjudicationResult(kept=[real_edit, self_query], queries=[routed_query])

    edits, queries = partition_for_deliverable(result)
    assert [f.id for f in edits] == ["g-1"]
    assert {f.id for f in queries} == {"g-2", "g-3"}


# ---------------------------------------------------------------------------
# build_manuscript_deliverable — the real thing, on the fixture book
# ---------------------------------------------------------------------------


def test_build_manuscript_deliverable_applies_edits_and_queries(tmp_path):
    ms = _fixture_manuscript()
    edit = GFinding(
        id="g-1", error_type="spelling",
        span=Span(TEH_PARA, TEH_START, TEH_START + 3),
        find="teh", replace="the",
        provenance=Provenance("docproof_ladder", 1, "fake-model", 0.0),
    )
    query = GFinding(
        id="g-2", error_type="continuity",
        span=Span("body-0001", 0, 7),
        find="Kathryn", replace="",
        note="possible continuity flag",
        provenance=Provenance("docproof_ladder", 1, "fake-model", 0.0),
    )
    stale = GFinding(
        id="g-3", error_type="typo",
        span=Span(TEH_PARA, 0, 11),
        find="zzzznotreal", replace="zzz",
        provenance=Provenance("docproof_ladder", 1, "fake-model", 0.0),
    )
    result = AdjudicationResult(kept=[edit], queries=[query, stale])

    out = tmp_path / "run"
    deliverable = build_manuscript_deliverable(
        result, ms, source_path=FIXTURE, cfg=_lean_cfg(), out_dir=out)

    outputs = deliverable.outputs
    assert outputs.applied >= 1
    assert outputs.queried >= 1
    assert outputs.reviewed_path.is_file()
    # The stale span never anchored — dropped, named, not fatal.
    assert any("g-3" in line for line in deliverable.dropped)
    assert not any("g-1" in line for line in deliverable.dropped)
    assert not any("g-2" in line for line in deliverable.dropped)


def test_build_manuscript_deliverable_refuses_a_seeded_manuscript(tmp_path):
    ms = _fixture_manuscript()
    seeded, _key = seed_copy(ms, 1, rng_seed=0)
    result = AdjudicationResult(kept=[], queries=[])

    with pytest.raises(AssertionError):
        build_manuscript_deliverable(
            result, seeded, source_path=FIXTURE, cfg=_lean_cfg(),
            out_dir=tmp_path / "run")


def test_build_manuscript_deliverable_with_nothing_kept_still_writes_a_document(tmp_path):
    """An adjudication that keeps nothing (everything routed to query, or the
    run planted no findings at all) still produces a real reviewed document —
    the deliverable's job is to exist, not to have found something."""
    ms = _fixture_manuscript()
    result = AdjudicationResult(kept=[], queries=[])
    out = tmp_path / "run"
    deliverable = build_manuscript_deliverable(
        result, ms, source_path=FIXTURE, cfg=_lean_cfg(), out_dir=out)
    assert deliverable.outputs.reviewed_path.is_file()
    assert deliverable.dropped == []
