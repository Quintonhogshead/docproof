"""Every pass that fails, is skipped, or runs only partially has to be LOUD —
recorded the moment it is caught, aggregated once, and shown where a user looks:
the summary.md banner, the web job card, and the completion email.

#132 built the core channel (CoverageLedger.degraded + record_degraded, the
job card, a summary line) for glossary / continuity / chapter-continuity / the
judge gates. These tests cover the passes it left silent — Sapling, fact-check,
LanguageTool, the batch discard, and the continuity SKIP — plus the run_health
aggregator and the leading summary banner this branch adds on top. The class of
bug guarded against is a review that LOOKS complete while a whole pass silently
did nothing.
"""
from __future__ import annotations

import itertools
import types

from docproof.config import Config
from docproof.models import (CoverageGap, CoverageLedger, DegradedPass,
                             DocumentModel, ParagraphRef, StageWarning, Usage)
from docproof.pipeline import continuity_findings
from docproof.providers.base import ProviderResult
from docproof.reporting import run_health, write_summary_md

from .fakes import USAGE, FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


# -- the ledger: StageWarning as a graded superset of #132's DegradedPass ------

def test_note_grades_the_shortfall_and_keeps_132_compat():
    cov = CoverageLedger()
    assert cov.any_degraded is False
    cov.note("Sapling", "the pass failed and did not run", "failed")
    cov.note("continuity read", "over the token limit, so it was skipped",
             "skipped")
    assert cov.any_degraded is True
    assert [w.label for w in cov.degraded] == ["Sapling", "continuity read"]
    assert cov.degraded[0].kind == "failed"
    # #132's DegradedPass name still resolves, and record_degraded still records
    # a plain failure — the call sites it wired (continuity/glossary/judges) are
    # untouched.
    assert StageWarning is DegradedPass
    cov.record_degraded("meaning-check gate", "12 changes applied unread")
    assert cov.degraded[-1].kind == "failed"
    assert cov.degraded[-1].line() == "meaning-check gate: 12 changes applied unread"


# -- run_health aggregation ---------------------------------------------------

def test_run_health_lists_degraded_worst_first():
    cov = CoverageLedger()
    cov.note("LanguageTool", "3 of 812 paragraph(s) could not be scanned",
             "partial")
    cov.note("Sapling", "the grammar/style pass failed and did not run", "failed")
    lines = run_health(cov)
    assert lines[0].startswith("Sapling:")          # failed sorts ahead of partial
    assert any("LanguageTool:" in ln and "812" in ln for ln in lines)


def test_run_health_reports_coverage_gaps_and_unruled():
    cov = CoverageLedger()
    cov.total = 4
    cov.gaps.append(CoverageGap("spelling", "chunk-1", ("body-0", "body-1")))
    cov.unruled.append(types.SimpleNamespace(lost=2, label="confirm",
                                             summary=lambda: "2 lost"))
    lines = run_health(cov)
    assert any("could not be reviewed" in ln for ln in lines)
    assert any("never got a verdict" in ln for ln in lines)


def test_run_health_reports_audit_smoothing_chapter_and_judge():
    audit = types.SimpleNamespace(ran=True, passed=False,
                                  summary=lambda: "3 paragraph(s) changed")
    smoothing = types.SimpleNamespace(windows_failed=2, windows=10, unjudged=5)
    chapter = types.SimpleNamespace(read_failed=1, unjudged=0)
    judge = types.SimpleNamespace(
        checked=8, unread=2,
        spec=types.SimpleNamespace(label="meaning check"))
    lines = run_health(None, audit, smoothing, chapter, [judge])
    joined = "\n".join(lines)
    assert "reject-all audit FAILED" in joined
    assert "language smoothing: 2 of 10" in joined
    assert "language smoothing: 5 suggestion(s) were never ruled on" in joined
    assert "chapter continuity: 1 chapter(s) could not be read" in joined
    assert "meaning check: 2 change(s) were applied WITHOUT being read" in joined


def test_run_health_is_empty_on_a_clean_run():
    audit = types.SimpleNamespace(ran=True, passed=True, summary=lambda: "")
    smoothing = types.SimpleNamespace(windows_failed=0, windows=10, unjudged=0)
    judge = types.SimpleNamespace(checked=8, unread=0,
                                  spec=types.SimpleNamespace(label="x"))
    assert run_health(CoverageLedger(), audit, smoothing, None, [judge]) == []


# -- the passes #132 left silent now record -----------------------------------

def test_factcheck_read_failure_is_recorded_in_coverage():
    from docproof.factcheck import build_factcheck
    cov = CoverageLedger()
    prov = FakeProvider([ProviderResult(stop_reason="error", error="overloaded",
                                        usage=USAGE)])
    build_factcheck([_para("body-0", "text")], prov, model="claude-fable-5",
                    max_tokens=8000, usage=Usage(), coverage=cov)
    assert [w.label for w in cov.degraded] == ["fact check"]
    assert "overloaded" in cov.degraded[0].reason


def test_languagetool_not_installed_is_recorded(monkeypatch):
    from docproof import languagetool
    monkeypatch.setattr(languagetool, "AVAILABLE", False)
    cov = CoverageLedger()
    assert languagetool.propose([_para("body-0", "some prose")], coverage=cov) == []
    assert [w.label for w in cov.degraded] == ["LanguageTool"]
    assert cov.degraded[0].kind == "skipped"


def test_sapling_malformed_edits_are_counted_via_stats(monkeypatch):
    from docproof import sapling

    class _Resp:
        status_code = 200

        def json(self):
            return {"edits": [
                {"sentence_start": 0, "start": 0, "end": 3, "replacement": "the"},
                {"sentence_start": 0, "replacement": "no offsets"},   # malformed
            ]}

    monkeypatch.setattr(sapling.httpx, "post", lambda *a, **k: _Resp())
    stats: dict = {}
    sapling.check("the text here", "key", stats=stats)
    assert stats.get("dropped_malformed") == 1


def test_sapling_enabled_without_a_key_is_recorded(monkeypatch):
    from docproof.pipeline import _sapling_findings
    monkeypatch.delenv("SAPLING_API_KEY", raising=False)
    cfg = Config()
    cfg.sapling.enabled = True
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "some prose"),))
    prepared = types.SimpleNamespace(doc=doc)
    cov = CoverageLedger()
    out = _sapling_findings(cfg, prepared, Usage(), coverage=cov)
    assert out == []
    assert [w.label for w in cov.degraded] == ["Sapling"]
    assert cov.degraded[0].kind == "skipped"
    assert "SAPLING_API_KEY" in cov.degraded[0].reason


def test_continuity_over_token_limit_is_recorded_as_skipped():
    # #132 records a continuity read that FAILED; a read never MADE because the
    # book is over the ceiling is the same hole to the reader — this branch makes
    # that loud too.
    cfg = Config()
    cfg.continuity.enabled = True
    cfg.continuity.max_input_tokens = 1          # force the skip
    cfg.continuity.calendar_check = False
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "some prose here, plenty."),))
    prepared = types.SimpleNamespace(doc=doc, whole_document=True)
    cov = CoverageLedger()
    out = continuity_findings(cfg, prepared, itertools.count(1), Usage(),
                              lambda c: FakeProvider(), coverage=cov)
    assert out == []
    assert [w.label for w in cov.degraded] == ["continuity read"]
    assert cov.degraded[0].kind == "skipped"


# -- the summary.md banner leads the report -----------------------------------

def test_summary_md_leads_with_a_degraded_banner(tmp_path):
    from docproof.formats import DOCX
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))
    cov = CoverageLedger()
    cov.total = 1
    cov.note("Sapling", "the grammar/style pass failed (network) and did not run",
             "failed")
    write_summary_md(tmp_path / "summary.md", doc=doc, findings=[],
                     usage=Usage(), cfg=Config(), applied_ids=(), fmt=DOCX,
                     coverage=cov)
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Run health" in text
    assert "this review is degraded" in text
    assert "Sapling: the grammar/style pass failed" in text
    # The banner leads: it must sit above the Coverage section, not after it.
    assert text.index("Run health") < text.index("## Coverage")


def test_summary_md_vouches_for_a_clean_run(tmp_path):
    from docproof.formats import DOCX
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))
    cov = CoverageLedger()
    cov.total = 1                                   # reviewed, no gaps, no notes
    write_summary_md(tmp_path / "summary.md", doc=doc, findings=[],
                     usage=Usage(), cfg=Config(), applied_ids=(), fmt=DOCX,
                     coverage=cov)
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "All enabled passes ran to completion" in text
    assert "this review is degraded" not in text
