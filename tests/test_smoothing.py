"""The line-editing pass: suggestions that can never become corrections.

Everything here defends one claim — that a smoothing suggestion reaches the
author as a question and nothing else, whatever the judge said about it. The
rest guards the restraint the pass promises: dialogue left alone, the author's
own words left alone, the volume capped and the cap reported.

See docproof/smoothing.py and docproof.pipeline._smoothing_findings.
"""
from __future__ import annotations

from docproof.config import Config
from docproof.models import DocumentModel, Finding, ParagraphRef, Usage
from docproof.pipeline import Prepared, _smoothing_findings
from docproof.providers.base import ProviderResult
from docproof.smoothing import (cap_for, margin_note, quote_spans,
                                rank_and_cap, touches_lexicon)
from docproof.validator import validate_findings

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def _prepared(*paras: ParagraphRef) -> Prepared:
    doc = DocumentModel(source_path="x.docx", paragraphs=tuple(paras))
    return Prepared(pkg=None, doc=doc, chunks=[], groups=[], fmt=None)


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.smoothing.enabled = True
    for k, v in kw.items():
        setattr(cfg.smoothing, k, v)
    return cfg


def _suggest(*items) -> ProviderResult:
    return ProviderResult(parsed={"suggestions": list(items)})


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


def _s(para_id, quote, suggestion, category="tighten", rationale="reads tighter"):
    return {"para_id": para_id, "quote": quote, "suggestion": suggestion,
            "category": category, "rationale": rationale}


def _run(monkeypatch, cfg, prepared, results):
    """Run the pass with a scripted provider for both stages."""
    prov = FakeProvider(results)
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)
    findings, report = _smoothing_findings(cfg, prepared, Usage())
    return findings, report, prov


# --- the invariant ------------------------------------------------------------

def test_a_smoothing_suggestion_is_never_a_tracked_change(monkeypatch):
    """The pass's whole reason to exist safely. A smoothing has no right answer,
    so however sure the judge is, it may only ever ask."""
    p = _para("body-0", "She walked over to the door in a very quiet way.")
    findings, _report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "in a very quiet way", "quietly")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"}),
    ])
    assert len(findings) == 1
    assert findings[0].force_query is True

    # And it survives arbitration as a question, not an edit.
    validated = validate_findings(findings, _prepared(p).doc, "low")
    assert [f.status for f in validated] == ["query"]


def test_even_a_high_confidence_verdict_cannot_reach_validated(monkeypatch):
    """Belt and braces on the same claim, stated the way it would break: the
    routing must not go through a confidence threshold, because a threshold has
    a value that clears it."""
    p = _para("body-0", "He was walking slowly down the long empty road.")
    findings, _r, _prov = _run(monkeypatch, _cfg(min_confidence="low"),
                               _prepared(p), [
        _suggest(_s("body-0", "was walking", "walked", category="aspect")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"}),
    ])
    validated = validate_findings(findings, _prepared(p).doc, "low")
    assert validated and all(f.status != "validated" for f in validated)


def test_a_suggestion_quotes_its_sentence_not_the_whole_paragraph(monkeypatch):
    """A query is anchored on what it quotes, so quoting the paragraph would
    highlight the paragraph. An author needs to be told where to look."""
    text = ("The rain had stopped. She walked over to the door in a very quiet "
            "way. Outside, the street was empty.")
    p = _para("body-0", text)
    findings, _r, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "in a very quiet way", "quietly")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"}),
    ])
    quoted = findings[0].original_text
    assert "She walked over to the door" in quoted
    assert "The rain had stopped" not in quoted
    assert "the street was empty" not in quoted


def test_the_suggested_wording_reaches_the_margin(monkeypatch):
    """`queries.query_text` renders a query as its explanation ALONE — the
    `Suggested:` line other statuses get is unreachable for a query. So if the
    wording is not in the explanation, the author never sees what to consider."""
    from docproof.queries import query_text
    p = _para("body-0", "She walked over to the door in a very quiet way.")
    findings, _r, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "in a very quiet way", "quietly")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"}),
    ])
    validated = validate_findings(findings, _prepared(p).doc, "low")
    assert "quietly" in query_text(validated[0])


def test_two_suggestions_on_one_sentence_stay_two_questions(monkeypatch):
    """The validator keys a force_query finding partly on corrected_text. A pass
    that emitted the original unchanged would collapse both into one and drop
    the second as a duplicate."""
    p = _para("body-0", "He was walking slowly in a very quiet way indeed.")
    # A generous cap: this test is about the validator's duplicate key, and the
    # default cap would legitimately drop the second on a nine-word document.
    findings, _r, _prov = _run(monkeypatch, _cfg(max_per_1000_words=500),
                               _prepared(p), [
        _suggest(_s("body-0", "was walking", "walked", category="aspect"),
                 _s("body-0", "in a very quiet way", "quietly")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"},
                  {"index": 2, "is_error": True, "confidence": "high"}),
    ])
    validated = validate_findings(findings, _prepared(p).doc, "low")
    assert [f.status for f in validated] == ["query", "query"]


# --- restraint ----------------------------------------------------------------

def test_off_by_default_spends_nothing():
    cfg = Config()                                  # smoothing defaults off
    p = _para("body-0", "She walked over to the door in a very quiet way.")
    # No provider is patched: if the pass ran at all, build_provider would fire.
    findings, report = _smoothing_findings(cfg, _prepared(p), Usage())
    assert findings == [] and report.proposed == 0


def test_dialogue_is_left_alone_by_default(monkeypatch):
    """A character's diction is theirs. In dialogue, awkward is often the point."""
    p = _para("body-0", '"I was just sort of going over there," he said.')
    findings, report, prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "just sort of going", "going")),
    ])
    assert findings == [] and report.proposed == 0
    # The judge was never asked — the filter runs before the paid call.
    assert len(prov.calls) == 1


def test_dialogue_can_be_opted_into(monkeypatch):
    p = _para("body-0", '"I was just sort of going over there," he said.')
    findings, _r, _prov = _run(monkeypatch, _cfg(include_dialogue=True),
                               _prepared(p), [
        _suggest(_s("body-0", "just sort of going", "going")),
        _verdicts({"index": 1, "is_error": True, "confidence": "high"}),
    ])
    assert len(findings) == 1


def test_an_author_coinage_is_never_offered_a_smoothing(monkeypatch):
    """The lexicon is what the spell scan agreed to take on trust. A pass that
    offers to tighten the sentence a coined word lives in is one keystroke from
    renaming it."""
    import dataclasses

    from docproof.spellscan import SpellScan
    p = _para("body-0", "The zorrillo moved in a very quiet way across the sand.")
    prepared = dataclasses.replace(_prepared(p),
                                   spell=SpellScan(lexicon=("zorrillo",)))
    findings, report, prov = _run(monkeypatch, _cfg(), prepared, [
        _suggest(_s("body-0", "The zorrillo moved in a very quiet way",
                    "The zorrillo crossed quietly")),
    ])
    assert findings == [] and report.proposed == 0
    assert len(prov.calls) == 1                     # judge never paid for


def test_a_suggestion_that_does_not_anchor_is_dropped_not_guessed(monkeypatch):
    p = _para("body-0", "She walked over to the door.")
    findings, report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "a sentence that is not in the paragraph", "x")),
    ])
    assert findings == [] and report.proposed == 0


def test_an_oversized_rewrite_is_dropped_before_the_judge(monkeypatch):
    """Queries skip the validator's edit guard, so the minimal-edit scale is
    kept here or not at all."""
    long_quote = ("She walked over to the door and stood there for a while "
                  "wondering whether anyone would come at all")
    p = _para("body-0", long_quote + ".")
    findings, report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", long_quote,
                    "She waited at the door, wondering if anyone would come")),
    ])
    assert findings == [] and report.proposed == 0


def test_the_judge_can_refuse_a_suggestion(monkeypatch):
    p = _para("body-0", "She walked over to the door in a very quiet way.")
    findings, report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _suggest(_s("body-0", "in a very quiet way", "quietly")),
        _verdicts({"index": 1, "is_error": False, "confidence": "high"}),
    ])
    assert findings == [] and report.proposed == 1 and report.kept == 0


# --- the cap ------------------------------------------------------------------

def test_cap_scales_with_the_manuscript_and_never_reaches_zero():
    assert cap_for(10_000, 3.0) == 30
    assert cap_for(1_000, 3.0) == 3
    assert cap_for(50, 3.0) == 1              # a short piece still gets one


def _f(conf: str, n: int) -> Finding:
    return Finding(finding_id=f"sm-{n:04d}", chunk_id="smoothing",
                   para_id="body-0", error_type="smoothing",
                   original_text="x", occurrence=1, corrected_text="y",
                   explanation="", confidence=conf, force_query=True)


def test_the_cap_keeps_the_surest_and_counts_the_rest():
    findings = [_f("low", 1), _f("high", 2), _f("medium", 3), _f("high", 4)]
    kept, withheld = rank_and_cap(findings, cap=2, min_confidence="low")
    assert [f.finding_id for f in kept] == ["sm-0002", "sm-0004"]
    assert withheld == 2


def test_the_confidence_floor_drops_before_the_cap_counts():
    """A suggestion below the floor was never eligible, so it is not 'withheld' —
    reporting it as withheld would overstate what the cap cost the author."""
    findings = [_f("low", 1), _f("high", 2)]
    kept, withheld = rank_and_cap(findings, cap=5, min_confidence="medium")
    assert [f.finding_id for f in kept] == ["sm-0002"] and withheld == 0


def test_the_cap_reports_what_it_withheld(monkeypatch):
    """A cap the author cannot see is indistinguishable from a pass that found
    little, and those are very different facts about their manuscript."""
    p = _para("body-0", "He was walking slowly in a very quiet way indeed.")
    findings, report, _prov = _run(
        monkeypatch, _cfg(max_per_1000_words=0.1), _prepared(p), [
            _suggest(_s("body-0", "was walking", "walked", category="aspect"),
                     _s("body-0", "in a very quiet way", "quietly")),
            _verdicts({"index": 1, "is_error": True, "confidence": "high"},
                      {"index": 2, "is_error": True, "confidence": "high"}),
        ])
    assert report.cap == 1
    assert len(findings) == 1 and report.kept == 1 and report.withheld == 1


def test_the_withheld_count_reaches_summary_md(tmp_path):
    from docproof.reporting import write_summary_md
    from docproof.smoothing import SmoothingReport
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))

    from docproof.formats import DOCX
    write_summary_md(tmp_path / "summary.md", doc=doc, findings=[],
                     usage=Usage(), cfg=Config(), applied_ids=(), fmt=DOCX,
                     smoothing=SmoothingReport(proposed=9, kept=3, withheld=6,
                                               cap=3))
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "6 further suggestion(s) withheld" in text


# --- unit: the deterministic helpers ------------------------------------------

def test_quote_spans_finds_double_quoted_dialogue():
    text = 'She said, "I was going there," and left.'
    assert [text[a:b] for a, b in quote_spans(text, '”"')] == \
        ['"I was going there,"']


def test_quote_spans_does_not_mistake_an_apostrophe_for_a_uk_quote():
    """The U.K. variant's closing mark is the apostrophe character, so a
    possessive or a contraction must not end a quote — or open one."""
    text = "Sarah's brother didn't go, and that was that."
    assert quote_spans(text, "’'") == []


def test_quote_spans_reads_uk_single_quoted_dialogue():
    text = "'I didn't go,' she said."
    spans = quote_spans(text, "’'")
    assert spans and text[spans[0][0]:spans[0][1]] == "'I didn't go,'"


def test_touches_lexicon_matches_a_word_anywhere_in_the_span():
    """Deliberately broader than Sapling's filter, which only compares an edit's
    whole original against the lexicon."""
    assert touches_lexicon("the zorrillo moved", {"zorrillo"}) is True
    assert touches_lexicon("Zorrillo's tail,", {"zorrillo"}) is True
    assert touches_lexicon("the animal moved", {"zorrillo"}) is False


def test_the_margin_note_announces_itself_as_a_suggestion():
    note = margin_note("quietly", "reads tighter")
    assert note.startswith("Consider:")
    assert "quietly" in note and "reads tighter" in note


# --- isolation ----------------------------------------------------------------

def test_a_rejudge_never_pays_for_smoothing_again():
    """A re-judge inherits the original run's config; the suggestions are already
    in the record, so paying again would buy a second, differently-worded set of
    the same questions."""
    from docproof.rejudge import _gates_only
    cfg = Config()
    cfg.smoothing.enabled = True
    assert _gates_only(cfg).smoothing.enabled is False


def test_a_free_rebuild_never_pays_for_smoothing_again():
    from app.jobs import _free_finish
    cfg = Config()
    cfg.smoothing.enabled = True
    _free_finish(cfg)
    assert cfg.smoothing.enabled is False


def test_a_continuity_only_run_does_not_smooth():
    """Continuity-only promises one whole-book read, not a line edit."""
    import inspect

    import app.jobs as jobs
    src = inspect.getsource(jobs.JobRunner.config_for)
    assert '"smoothing"' in src


def test_the_eval_runner_disables_smoothing():
    from docproof.eval.runner import _eval_config
    cfg = Config()
    cfg.smoothing.enabled = True
    assert _eval_config(cfg).smoothing.enabled is False
