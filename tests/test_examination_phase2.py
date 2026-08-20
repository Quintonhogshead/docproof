from pathlib import Path

import pytest
from pydantic import ValidationError

from docproof.batch import _assemble, custom_id
from docproof.checkpoint import Checkpoint
from docproof.config import Config, load_config
from docproof.error_registry import load_error_types
from docproof.examination import prepare_shadow
from docproof.models import Chunk, DocumentModel, Finding, ParagraphRef
from docproof.pipeline import Prepared, run_sync
from docproof.providers import ProviderResult
from docproof.site_models import LedgerState
from docproof.spellscan import SpellScan

from .fakes import FakeProvider, USAGE


ERROR_DIR = Path(__file__).parent.parent / "config" / "error_types"


def _cfg() -> Config:
    return Config(
        error_types=["spelling"], sweeps=[],
        examination_graph={
            "enabled": True,
            "production_verdicts": True,
        })


def _run(paragraphs):
    cfg = _cfg()
    doc = DocumentModel("book.docx", tuple(paragraphs))
    run = prepare_shadow(
        cfg, doc, paragraphs=paragraphs, sweep_findings=[],
        consistency_findings=[], spell=SpellScan(available=False),
        adjudicate_candidates=[])
    assert run is not None
    return cfg, doc, run


def _prepared(cfg, doc, run, paragraphs):
    group = list(load_error_types(ERROR_DIR, ["spelling"]).values())
    chunk = Chunk("chunk-000", tuple(paragraphs), 20)
    return Prepared(
        pkg=None, doc=doc, chunks=[chunk], groups=[group], fmt=None,
        whole_document=False, examination=run)


def test_explicit_clean_receipt_settles_a_model_obligation_as_passed(tmp_path):
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "A clean line.", "Normal")
    cfg, doc, run = _run([para])
    prepared = _prepared(cfg, doc, run, [para])
    provider = FakeProvider([ProviderResult(
        parsed={"findings": [],
                "reviewed_paragraph_ids": [para.para_id]},
        usage=USAGE)])

    findings, usage = run_sync(cfg, prepared, provider)

    obligation = run.model_obligations[(para.para_id, "spelling")]
    assert findings == []
    assert usage.api_calls == 1
    assert run.ledger.state(obligation) == LedgerState.MODEL_PASSED
    report = run.production_verdicts_report(True)
    assert report["expected_sites"] == report["explicit_passes"] == 1
    assert report["pending_sites"] == 0
    assert report["coverage_percent"] == 100.0

    written, _ledger, _markdown = run.write(
        tmp_path, cfg.examination_graph, source=doc.source_path)
    assert written["scope"]["phase"] == 2
    assert (
        "explicit pass/error receipts from production model prompts"
        in written["scope"]["implemented"])


def test_missing_receipt_stays_pending_while_a_finding_is_error_evidence():
    first = ParagraphRef(
        "body-0000", "word/document.xml", "body", "Teh first line.", "Normal")
    second = ParagraphRef(
        "body-0001", "word/document.xml", "body", "A second line.", "Normal")
    _cfg_obj, _doc, run = _run([first, second])
    finding = Finding(
        "f-0001", "chunk-000", first.para_id, "spelling", first.text, 1,
        "The first line.", "Teh is misspelled.", "high")

    run.expect_production_response(
        "p0-chunk-000", ("spelling",), (first, second))
    run.observe_production_response(
        "p0-chunk-000", ("spelling",), (first, second),
        (first.para_id,), [finding])
    run.finalize_production_verdicts()

    first_site = run.model_obligations[(first.para_id, "spelling")]
    second_site = run.model_obligations[(second.para_id, "spelling")]
    assert run.ledger.state(first_site) == LedgerState.MODEL_CONFIRMED
    assert run.ledger.state(second_site) == LedgerState.NEEDS_JUDGMENT
    report = run.production_verdicts_report(True)
    assert report["explicit_errors"] == 1
    assert report["pending_sites"] == 1
    assert report["complete_responses"] == 0
    assert report["contract_issues"][0]["missing_paragraph_ids"] == [
        second.para_id]
    assert "incomplete for 1 production response" in run.failure_warnings()[0]


def test_a_legacy_receipt_failure_never_discards_production_findings():
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "Teh first line.", "Normal")
    cfg, doc, run = _run([para])
    prepared = _prepared(cfg, doc, run, [para])
    provider = FakeProvider([ProviderResult(parsed={"findings": [{
        "para_id": para.para_id,
        "error_type": "spelling",
        "original_text": para.text,
        "corrected_text": "The first line.",
        "confidence": "high",
        "explanation": "Teh is misspelled.",
    }]}, usage=USAGE)])

    findings, _usage = run_sync(cfg, prepared, provider)

    assert len(findings) == 1
    assert findings[0].corrected_text == "The first line."
    obligation = run.model_obligations[(para.para_id, "spelling")]
    assert run.ledger.state(obligation) == LedgerState.MODEL_CONFIRMED
    assert run.production_verdicts_report(True)["contract_issues"]


def test_a_pass_requires_every_expected_repeat_to_return_a_receipt():
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "A clean line.", "Normal")
    _cfg_obj, _doc, run = _run([para])
    for response_id in ("p0-chunk-000", "p1-chunk-000"):
        run.expect_production_response(response_id, ("spelling",), (para,))
    run.observe_production_response(
        "p0-chunk-000", ("spelling",), (para,), (para.para_id,), [])
    run.finalize_production_verdicts()

    obligation = run.model_obligations[(para.para_id, "spelling")]
    assert run.ledger.state(obligation) == LedgerState.NEEDS_JUDGMENT
    assert run.production_verdicts_report(True)["pending_sites"] == 1


def test_production_receipt_replays_with_a_paid_checkpoint(tmp_path):
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "A clean line.", "Normal")
    cfg, doc, first = _run([para])
    checkpoint = Checkpoint(
        tmp_path / "checkpoint.json", fingerprint={"phase": 2})
    checkpoint.load()
    provider = FakeProvider([ProviderResult(
        parsed={"findings": [],
                "reviewed_paragraph_ids": [para.para_id]},
        usage=USAGE)])
    run_sync(cfg, _prepared(cfg, doc, first, [para]), provider,
             checkpoint=checkpoint)
    assert len(provider.calls) == 1

    cfg2, doc2, resumed = _run([para])
    replay = Checkpoint(
        tmp_path / "checkpoint.json", fingerprint={"phase": 2})
    assert replay.load() == 1
    no_call = FakeProvider([])
    run_sync(cfg2, _prepared(cfg2, doc2, resumed, [para]), no_call,
             checkpoint=replay)

    obligation = resumed.model_obligations[(para.para_id, "spelling")]
    assert no_call.calls == []
    assert resumed.ledger.state(obligation) == LedgerState.MODEL_PASSED


def test_batch_collection_projects_the_same_explicit_pass():
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "A clean line.", "Normal")
    cfg, doc, run = _run([para])
    prepared = _prepared(cfg, doc, run, [para])
    response_id = custom_id(0, prepared.chunks[0].chunk_id)

    findings, usage = _assemble(cfg, prepared, {response_id: ProviderResult(
        parsed={"findings": [],
                "reviewed_paragraph_ids": [para.para_id]},
        usage=USAGE)})

    obligation = run.model_obligations[(para.para_id, "spelling")]
    assert findings == []
    assert usage.api_calls == 1
    assert run.ledger.state(obligation) == LedgerState.MODEL_PASSED


def test_phase_two_output_contract_requires_the_compact_receipt():
    from docproof.analyzer import build_output_model, build_system_prompt

    model = build_output_model(("spelling",), explicit_verdicts=True)
    with pytest.raises(ValidationError):
        model.model_validate({"findings": []})
    parsed = model.model_validate({
        "findings": [], "reviewed_paragraph_ids": ["body-0000"]})
    assert parsed.reviewed_paragraph_ids == ["body-0000"]

    error_type = next(iter(load_error_types(
        ERROR_DIR, ["spelling"]).values()))
    prompt = build_system_prompt([error_type], explicit_verdicts=True)
    assert "EXPLICIT COVERAGE RECEIPT" in prompt
    assert "Do not include ids from the read-only <context>" in prompt


def test_phase_two_cannot_be_enabled_without_model_obligations():
    with pytest.raises(ValidationError, match="requires model_obligations"):
        Config(examination_graph={
            "enabled": True,
            "model_obligations": False,
            "production_verdicts": True,
        })


def test_phase_two_deployment_brake_wins_for_loaded_and_stored_configs(
        monkeypatch):
    monkeypatch.setenv("DOCPROOF_EXAMINATION_PRODUCTION_VERDICTS", "0")
    shipped = load_config(Path(__file__).parent.parent / "config" / "default.yaml")
    assert shipped.examination_graph.enabled is True
    assert shipped.examination_graph.production_verdicts is False

    # Stored jobs bypass load_config, so the analyzer's runtime check must also
    # restore the finding-only contract without disabling the phase-one ledger.
    cfg = _cfg()
    para = ParagraphRef(
        "body-0000", "word/document.xml", "body", "A clean line.", "Normal")
    _cfg_obj, doc, run = _run([para])
    prepared = _prepared(cfg, doc, run, [para])
    provider = FakeProvider([ProviderResult(parsed={"findings": []}, usage=USAGE)])
    findings, _usage = run_sync(cfg, prepared, provider)
    assert findings == []
    assert "reviewed_paragraph_ids" not in provider.calls[0]["schema"][
        "properties"]
    assert run.production_expected == {}
