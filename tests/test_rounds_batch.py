"""Multi-round review on the batch path (design stage E2-batch).

Drives the real submit -> poll -> collect_findings loop through the fake
provider's batch methods, with the judge and fold happening between rounds in
process. The fake batch completes on the first poll, so no sleeping. Checks the
same deliverable invariants as the sync adapter plus that each round actually
rides a submitted batch.
"""
from __future__ import annotations

import re
from pathlib import Path

import docx

from docproof.providers import ProviderResult
from docproof.reassembler import paragraph_view_text
from docproof.rounds import run_batch_rounds
from docproof.utils.xml_helpers import DocxPackage, walk_package
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
    cfg.error_types = ["comma_splice"]
    cfg.sweeps = []
    for block in (cfg.glossary, cfg.storysheet, cfg.rewrite, cfg.languagetool,
                  cfg.adjudicate, cfg.consistency, cfg.spellcheck):
        block.enabled = False
    return cfg


def test_batch_two_rounds_apply_edits_from_both(tmp_path, cfg):
    _minimal(cfg)
    cfg.rounds.count = 2
    src = _docx(tmp_path, "the cat sat", "He ran he fell")
    # round 1's batch yields the P0 fix; round 2's yields the P1 fix
    review = FakeProvider([
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat"),
        finding_result(para_id="body-0001", error_type="comma_splice",
                       original="He ran he fell", corrected="He ran, he fell"),
    ])
    out = run_batch_rounds(cfg, str(src), ERROR_DIR, str(tmp_path / "jobs"),
                           out_dir=tmp_path, review_provider=review,
                           judge_provider=_ApproveJudge(), poll_interval=0)
    reject, accept = _views(out.reviewed_path)
    assert reject == {"body-0000": "the cat sat", "body-0001": "He ran he fell"}
    assert accept == {"body-0000": "the dog sat", "body-0001": "He ran, he fell"}
    # each round submitted its own batch
    submits = [c for c in review.calls if c.get("batch")]
    assert len(submits) == 2


def test_batch_on_progress_marks_each_round_boundary(tmp_path, cfg):
    # A vendor batch has no per-section signal while it runs, so the driver
    # reports only the round boundaries — with the counts zeroed.
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
    run_batch_rounds(cfg, str(src), ERROR_DIR, str(tmp_path / "jobs"),
                     out_dir=tmp_path, review_provider=review,
                     judge_provider=_ApproveJudge(), poll_interval=0,
                     on_progress=lambda *a: calls.append(a))
    assert calls == [(1, 2, 0, 0), (2, 2, 0, 0)]


def test_batch_early_stops_on_a_dry_round(tmp_path, cfg):
    _minimal(cfg)
    cfg.rounds.count = 3
    src = _docx(tmp_path, "the cat sat", "second line")
    review = FakeProvider([                            # only round 1 finds anything
        finding_result(para_id="body-0000", error_type="comma_splice",
                       original="the cat sat", corrected="the dog sat")])
    out = run_batch_rounds(cfg, str(src), ERROR_DIR, str(tmp_path / "jobs"),
                           out_dir=tmp_path, review_provider=review,
                           judge_provider=_ApproveJudge(), poll_interval=0)
    _, accept = _views(out.reviewed_path)
    assert accept["body-0000"] == "the dog sat"
    submits = [c for c in review.calls if c.get("batch")]
    assert len(submits) == 2                           # round 1 + one dry round, then stop
