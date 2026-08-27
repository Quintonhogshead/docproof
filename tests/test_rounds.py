"""Multi-round review orchestration (design doc stage E, core).

`run_rounds` is exercised end to end with a fake reviewer and a fake judge, but
real edit layers, real validation, and the real reassembler — so the test proves
the whole loop by building an actual tracked-change .docx and checking the
deliverable invariants: rejecting every change reproduces the original, accepting
every change gives the fully-corrected text, a judge-rejected fix never appears,
a downgraded finding becomes a margin comment, and a round-2 edit on text a
round-1 edit introduced composes into a single change against the original.
"""
from __future__ import annotations

import re

import docx

from docproof.config import Config
from docproof.models import DocumentModel, Finding, ParagraphRef, Usage
from docproof.providers import ProviderResult
from docproof.reassembler import apply_tracked_changes, paragraph_view_text
from docproof.rounds import RoundReview, _round_config, run_rounds
from docproof.utils.xml_helpers import DocxPackage, paragraph_text, walk_package
from docproof.validator import validate_findings
from tests.fakes import USAGE


def _f(fid, para, original, corrected, etype="typo"):
    return Finding(fid, "rounds", para, etype, original, 1, corrected,
                   f"{etype} fix", "high")


class _JudgeProvider:
    """Verdicts scripted per finding_id (approve for anything unset); reads the
    prompt so per-paragraph calls each answer for the ids they were sent."""

    name = "fake-judge"

    def __init__(self, script):
        self.script = script

    def complete_structured(self, *, user, **kwargs):
        ids = re.findall(r"finding_id=(\S+)", user)
        verdicts = [{"finding_id": fid,
                     "verdict": self.script.get(fid, ("approve", ""))[0],
                     "reason": self.script.get(fid, ("approve", ""))[1]}
                    for fid in ids]
        return ProviderResult(parsed={"verdicts": verdicts}, usage=USAGE)


def _pkg_and_doc(tmp_path, texts):
    d = docx.Document()
    for t in texts:
        d.add_paragraph(t)
    path = tmp_path / "orig.docx"
    d.save(path)
    pkg = DocxPackage(path)
    doc = DocumentModel("orig.docx", tuple(
        ParagraphRef(wp.para_id, wp.part, "body", paragraph_text(wp.element),
                     "Normal") for wp in walk_package(pkg)))
    return pkg, doc


def _reviews():
    """Round 1 reviews the original; round 2 reviews it as round 1 left it.
    P0: cat -> dog (r1) then dog -> fox (r2) — composes to one cat -> fox change.
    P1: comma inserted (r1, kept) + terminal period (sweep) + a rejected 'felled';
    r2 flags a style change that the judge downgrades to a margin query."""
    def review(k, edits_by_para):
        if k == 1:
            return RoundReview(
                model_findings=[
                    _f("f-cat", "body-0000", "the cat", "the dog"),
                    _f("f-comma", "body-0001", "He ran he fell", "He ran, he fell"),
                    _f("f-bad", "body-0001", "He ran he fell", "He ran he felled"),
                ],
                para_text={"body-0000": "the cat",
                           "body-0001": "He ran he fell"},
                sweep_findings=[_f("s-period", "body-0001", "He ran he fell",
                                   "He ran he fell.", etype="sweep_terminal_period")],
            )
        return RoundReview(
            model_findings=[
                _f("f-fox", "body-0000", "the dog", "the fox"),
                _f("f-style", "body-0001", "He ran, he fell.",
                   "He sprinted, he fell."),
            ],
            para_text={"body-0000": "the dog",
                       "body-0001": "He ran, he fell."},
        )
    return review


JUDGE = _JudgeProvider({"f-bad": ("reject", "Correct as written."),
                       "f-style": ("query", "Word choice — your call.")})


def _run(tmp_path):
    pkg, doc = _pkg_and_doc(tmp_path, ["the cat", "He ran he fell"])
    usage = Usage()
    result = run_rounds(doc, _reviews(), JUDGE, count=2,
                        judge_model="claude-opus-5", usage=usage)
    return pkg, doc, result


def test_deliverable_reject_all_reproduces_the_original(tmp_path):
    pkg, doc, result = _run(tmp_path)
    cfg = Config()
    validated = validate_findings(result.edits + result.queries, doc,
                                  cfg.min_confidence, query_types=frozenset(),
                                  format_types={}, edit_guard=cfg.edit_guard)
    apply_tracked_changes(pkg, doc, validated, cfg)
    reject = [paragraph_view_text(wp.element, "reject") for wp in walk_package(pkg)]
    assert reject == ["the cat", "He ran he fell"]         # every change rejects cleanly


def test_deliverable_accept_all_is_the_corrected_text(tmp_path):
    pkg, doc, result = _run(tmp_path)
    cfg = Config()
    validated = validate_findings(result.edits + result.queries, doc,
                                  cfg.min_confidence, query_types=frozenset(),
                                  format_types={}, edit_guard=cfg.edit_guard)
    apply_tracked_changes(pkg, doc, validated, cfg)
    accept = [paragraph_view_text(wp.element, "accept") for wp in walk_package(pkg)]
    assert accept == ["the fox", "He ran, he fell."]
    assert "felled" not in accept[1]                       # the rejected fix never landed


def test_cross_round_edit_composes_into_one_change(tmp_path):
    _, _, result = _run(tmp_path)
    p0 = result.layers["body-0000"]
    assert len(p0.edits) == 1                              # cat->dog->fox is one entry
    assert p0.edits[0].replacement == "fox"
    assert [c.finding_id for c in p0.edits[0].contributions] == ["f-cat", "f-fox"]


def test_rejected_fix_is_reported_not_applied(tmp_path):
    _, _, result = _run(tmp_path)
    assert [f.finding_id for f in result.rejected] == ["f-bad"]
    assert result.rounds_run == 2


def test_downgraded_finding_becomes_a_margin_query(tmp_path):
    pkg, doc, result = _run(tmp_path)
    assert [q.para_id for q in result.queries] == ["body-0001"]
    assert result.queries[0].force_query
    cfg = Config()
    validated = validate_findings(result.edits + result.queries, doc,
                                  cfg.min_confidence, query_types=frozenset(),
                                  format_types={}, edit_guard=cfg.edit_guard)
    stats = apply_tracked_changes(pkg, doc, validated, cfg)
    assert len(stats.queried) == 1                         # written as a comment, no edit


def test_early_stop_when_a_round_adds_too_little(tmp_path):
    pkg, doc = _pkg_and_doc(tmp_path, ["the cat", "He ran he fell"])

    def review(k, edits_by_para):
        if k == 1:
            return RoundReview(
                model_findings=[_f("f-cat", "body-0000", "the cat", "the dog")],
                para_text={"body-0000": "the cat", "body-0001": "He ran he fell"})
        # round 2 finds nothing new
        return RoundReview(model_findings=[],
                           para_text={"body-0000": "the dog",
                                      "body-0001": "He ran he fell"})

    result = run_rounds(doc, review, _JudgeProvider({}), count=5,
                        judge_model="m", min_new_edits=1, usage=Usage())
    assert result.rounds_run == 2                          # stopped after the dry round


def test_later_rounds_drop_the_whole_book_passes_when_reuse_is_on():
    """Round 1 runs the config as given; with reuse on, later rounds skip the
    whole-book re-reads and the mechanical-floor scan — round 1 caught those and
    its confirmed fixes make no new mechanical errors, so re-running the pass is
    only cost (a per-round single-core scan on a long book). Reuse off keeps
    everything, and the original config is never mutated."""
    cfg = Config()
    cfg.glossary.enabled = cfg.storysheet.enabled = cfg.languagetool.enabled = True

    assert _round_config(cfg, 1, True) is cfg              # round 1 untouched

    later = _round_config(cfg, 2, True)
    assert not later.languagetool.enabled
    assert not later.glossary.enabled and not later.storysheet.enabled
    # Splits happened once in round 1; a later round splitting would renumber
    # para_ids out from under the round-1 edit layers.
    assert not later.speaker_split.enabled

    kept = _round_config(cfg, 2, False)                    # reuse off: nothing dropped
    assert kept.languagetool.enabled

    assert cfg.languagetool.enabled                        # deep-copied, not mutated
