"""Below-gate promotion: a model edit under `min_confidence` gets one more pass
through the shared confirm valve, so a real dialogue-mechanics catch the type
prompts marked "low" can still become a tracked change instead of only ever a
margin comment. See docproof.pipeline._promote_low_confidence."""
from __future__ import annotations

import docproof.pipeline as pipeline
from docproof.config import Config
from docproof.models import DocumentModel, Finding, ParagraphRef, Usage
from docproof.pipeline import Prepared, _promote_low_confidence
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def _prepared(*paras: ParagraphRef) -> Prepared:
    doc = DocumentModel(source_path="x.docx", paragraphs=tuple(paras))
    return Prepared(pkg=None, doc=doc, chunks=[], groups=[], fmt=None)


def _low_edit(pid: str, quote: str, corrected: str) -> Finding:
    return Finding(finding_id="m-0001", chunk_id="c0", para_id=pid,
                   error_type="comma_error", original_text=quote, occurrence=1,
                   corrected_text=corrected, explanation="missing comma",
                   confidence="low")


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


def _cfg(**low) -> Config:
    cfg = Config()
    cfg.low_confidence.confirm = True
    for k, v in low.items():
        setattr(cfg.low_confidence, k, v)
    return cfg


def test_off_by_default_leaves_findings_untouched():
    p = _para("body-0", "Well I never said that.")
    f = _low_edit("body-0", p.text, "Well, I never said that.")
    cfg = Config()                                   # confirm defaults off
    survivors, promoted = _promote_low_confidence(cfg, _prepared(p), [f], Usage())
    assert survivors == [f] and promoted == []


def test_a_below_gate_edit_the_valve_affirms_is_promoted(monkeypatch):
    p = _para("body-0", "Well I never said that.")
    f = _low_edit("body-0", p.text, "Well, I never said that.")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "high"})])
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)

    survivors, promoted = _promote_low_confidence(
        _cfg(), _prepared(p), [f], Usage())
    # The low finding left the model set and came back as an applied edit.
    assert survivors == []
    assert len(promoted) == 1
    assert promoted[0].force_query is False
    assert promoted[0].corrected_text == "Well, I never said that."


def test_a_soft_affirmation_becomes_a_query_not_an_edit(monkeypatch):
    p = _para("body-0", "Well I never said that.")
    f = _low_edit("body-0", p.text, "Well, I never said that.")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "medium"})])
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)

    _, promoted = _promote_low_confidence(_cfg(), _prepared(p), [f], Usage())
    assert len(promoted) == 1 and promoted[0].force_query is True


def test_the_valve_can_drop_an_in_voice_below_gate_edit(monkeypatch):
    p = _para("body-0", "Aint nobody got time for that.")
    f = _low_edit("body-0", p.text, "Ain't nobody got time for that.")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": False, "confidence": "high"})])
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)

    survivors, promoted = _promote_low_confidence(
        _cfg(), _prepared(p), [f], Usage())
    # Not affirmed, and pulled from the model set: it neither edits nor lingers.
    assert survivors == [] and promoted == []


def test_above_gate_findings_never_enter_the_valve():
    p = _para("body-0", "He recieved the letter.")
    high = Finding(finding_id="m-0002", chunk_id="c0", para_id="body-0",
                   error_type="spelling", original_text=p.text, occurrence=1,
                   corrected_text="He received the letter.",
                   explanation="", confidence="high")
    # No provider is set: if the valve tried to run, build_provider would fire.
    survivors, promoted = _promote_low_confidence(
        _cfg(), _prepared(p), [high], Usage())
    assert survivors == [high] and promoted == []


def test_a_below_gate_edit_that_will_not_anchor_is_left_in_place():
    p = _para("body-0", "The quote does not match the paragraph.")
    f = _low_edit("body-0", "some other sentence entirely", "a correction")
    survivors, promoted = _promote_low_confidence(
        _cfg(), _prepared(p), [f], Usage())
    # It falls through so the validator can report it as a no-anchor, not vanish.
    assert survivors == [f] and promoted == []
