"""The synchronous multi-round adapter and the `--rounds` CLI (design stage E2).

These drive the REAL pipeline (prepare -> run_sync -> finish) with only the model
calls faked, so they check the wiring the pure orchestrator test cannot: working
documents are built and re-reviewed each round, whole-book reads are reused, and
the deliverable is assembled by `finish` (including its audit that rejecting
every change reproduces the original).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import docx
import pytest

from docproof.__main__ import main
from docproof.pipeline import JobCancelled
from docproof.providers import ProviderResult
from docproof.reassembler import paragraph_view_text
from docproof.rounds import run_sync_rounds
from docproof.utils.xml_helpers import DocxPackage, paragraph_text, walk_package
from tests.fakes import FakeProvider, finding_result

ERROR_DIR = Path(__file__).parent.parent / "config" / "error_types"


def _docx(tmp_path, *paras):
    d = docx.Document()
    for p in paras:
        d.add_paragraph(p)
    path = tmp_path / "src.docx"
    d.save(path)
    return path


def _views(path):
    pkg = DocxPackage(path)
    return ({wp.para_id: paragraph_view_text(wp.element, "reject")
             for wp in walk_package(pkg)},
            {wp.para_id: paragraph_view_text(wp.element, "accept")
             for wp in walk_package(pkg)})


class _ApproveJudge:
    name = "fake-judge"

    def complete_structured(self, *, user, **kwargs):
        ids = re.findall(r"finding_id=(\S+)", user)
        return ProviderResult(parsed={"verdicts": [
            {"finding_id": i, "verdict": "approve", "reason": ""} for i in ids]})


def _minimal(cfg):
    """A config that makes each round a single detector pass with no whole-book
    passes or sweeps, so run_sync makes exactly one (fake) call per round."""
    cfg.error_types = ["comma_splice"]
    cfg.sweeps = []
    for block in (cfg.glossary, cfg.storysheet, cfg.rewrite, cfg.languagetool,
                  cfg.adjudicate, cfg.consistency, cfg.spellcheck):
        block.enabled = False
    return cfg


# --- the adapter, two real rounds with injected fakes ------------------------

def test_two_rounds_apply_edits_from_both_rounds(tmp_path, cfg):
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    # round 1 fixes P0, round 2 fixes P1 (unchanged in round 2, so it anchors)
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    out = run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                          review_provider=review, judge_provider=_ApproveJudge())
    reject, accept = _views(out.reviewed_path)
    assert reject == {"body-0000": "the cat sat", "body-0001": "He ran he fell"}
    assert accept == {"body-0000": "the dog sat", "body-0001": "He ran, he fell"}
    assert review.calls and len(review.calls) == 2        # one detector call per round


def test_edit_introducing_a_collapsible_space_stays_in_step(tmp_path, cfg):
    """A round-1 edit that leaves a normalization-visible artifact (here a double
    space, from deleting a word) must not desync the working copy from the edit
    layer. Round 1's working document is normalized once (W0); later rounds read
    a copy rebuilt from the layers and must NOT be re-normalized, or the collapse
    would drift the re-ingested text from the layer's render and abort the run
    ("the working document is out of step"). Regression for that abort."""
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the big cat sat", "plain line")
    review = FakeProvider([
        # round 1: deleting "big" leaves "the  cat sat" (two spaces).
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the big cat sat", corrected="the  cat sat"),
        # round 2: a fresh edit on the SAME paragraph, anchored in the working
        # text as round 1 left it — this is what runs the fold-time invariant.
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the  cat sat", corrected="the  cat sat."),
    ])
    out = run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                          review_provider=review, judge_provider=_ApproveJudge())
    reject, accept = _views(out.reviewed_path)
    assert reject["body-0000"] == "the big cat sat"       # reject-all → original
    assert accept["body-0000"] == "the  cat sat."         # both rounds composed
    assert len(review.calls) == 2


def test_a_judge_rejection_keeps_the_original(tmp_path, cfg):
    _minimal(cfg)
    cfg.rounds.count = 2

    class _RejectP0:
        name = "fake-judge"

        def complete_structured(self, *, user, **kwargs):
            ids = re.findall(r"finding_id=(\S+)", user)
            # reject the P0 finding (its paragraph text is quoted), approve else
            reject = "cat" in user
            return ProviderResult(parsed={"verdicts": [
                {"finding_id": i, "verdict": "reject" if reject else "approve",
                 "reason": "no"} for i in ids]})

    src = _docx(tmp_path, "the cat sat", "plain line")
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat")])
    out = run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                          review_provider=review, judge_provider=_RejectP0())
    reject, accept = _views(out.reviewed_path)
    assert accept["body-0000"] == "the cat sat"           # rejected fix not applied
    assert out.applied == 0


def test_on_progress_reports_each_round_then_its_sections(tmp_path, cfg):
    """The driver announces each round as it starts (count zeroed), then
    forwards run_sync's per-call fold with the round number attached."""
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    calls = []
    run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                    review_provider=review, judge_provider=_ApproveJudge(),
                    on_progress=lambda *a: calls.append(a))
    # One detector call per round under _minimal, so each round is a boundary
    # announcement followed by a single 1-of-1 fold.
    assert calls == [(1, 2, 0, 0), (1, 2, 1, 1), (2, 2, 0, 0), (2, 2, 1, 1)]


def test_on_phase_cycles_the_stages_each_round_then_writes(tmp_path, cfg):
    """The stage story of a multi-round run: every round announces the working-
    document rebuild, its review steps, and the between-rounds judge; the final
    finish announces the assembly. The round number from on_progress is what
    keeps the repetition legible on a card."""
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    stages = []
    run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                    review_provider=review, judge_provider=_ApproveJudge(),
                    on_phase=stages.append)
    assert stages == ["preparing", "reviewing", "round_judge",
                      "preparing", "reviewing", "round_judge", "writing"]


def test_rounds_run_leaves_a_composed_snapshot(tmp_path, cfg):
    """The driver snapshots its composed findings before finish — the rebuild
    source for download-anyway when the audit fails after the paid rounds."""
    from docproof.checkpoint import finding_from_dict

    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                    review_provider=review, judge_provider=_ApproveJudge())
    snap = json.loads((tmp_path / "rounds" / "composed.json").read_text("utf-8"))
    findings = [finding_from_dict(d) for d in snap["findings"]]
    assert {f.para_id for f in findings} == {"body-0000", "body-0001"}
    assert snap["usage"]["api_calls"] >= 2       # both rounds' spend recorded


def test_sync_rounds_aborts_at_a_round_boundary(tmp_path, cfg):
    # should_cancel true at the round boundary stops before any model call.
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat")
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat")])
    with pytest.raises(JobCancelled):
        run_sync_rounds(cfg, str(src), ERROR_DIR, out_dir=tmp_path,
                        review_provider=review, judge_provider=_ApproveJudge(),
                        should_cancel=lambda: True)
    assert not review.calls          # aborted before the first round's review


# --- the CLI, offline via --mock-findings + --rounds -------------------------

def test_cli_rounds_with_mock_findings(tmp_path):
    src = _docx(tmp_path, "the cat sat", "second line")
    mocks = tmp_path / "mocks.json"
    mocks.write_text(json.dumps([
        {"para_id": "body-0000", "original_text": "the cat sat",
         "corrected_text": "the dog sat", "explanation": "x", "confidence": "high"},
    ]))
    rc = main(["review", str(src), "--mock-findings", str(mocks),
               "--rounds", "2", "--out", str(tmp_path)])
    assert rc == 0
    reviewed = tmp_path / "src - Atmosphere Press Proofreader.docx"
    assert reviewed.exists()
    for name in ("summary.md", "findings.json"):
        assert (tmp_path / name).exists(), name
    reject, accept = _views(reviewed)
    assert reject["body-0000"] == "the cat sat"           # fidelity: rejects to original
    assert accept["body-0000"] == "the dog sat"


def test_cli_rounds_1_is_the_ordinary_single_path(tmp_path):
    # --rounds 1 must not take the multi-round path (byte-for-byte single review)
    src = _docx(tmp_path, "the cat sat")
    mocks = tmp_path / "m.json"
    mocks.write_text(json.dumps([
        {"para_id": "body-0000", "original_text": "the cat sat",
         "corrected_text": "the dog sat", "explanation": "x", "confidence": "high"}]))
    rc = main(["review", str(src), "--mock-findings", str(mocks),
               "--rounds", "1", "--out", str(tmp_path)])
    assert rc == 0
    assert not (tmp_path / "rounds").exists()             # no working-doc scratch dir
