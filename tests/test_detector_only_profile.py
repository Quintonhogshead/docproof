"""The detector-only profile is identical on live and provider-batch paths."""
from __future__ import annotations

import json
from argparse import Namespace
from zipfile import ZipFile

from docproof import batch as batchlib
from docproof.__main__ import _configure, main
from docproof.config import Config, load_config
from docproof.pipeline import prepare
from docproof.profiles import DETECTOR_ONLY, QUERY_ONLY_TYPES, apply_profile
from .conftest import FIXTURES
from .fakes import ScriptedBatchProvider
from .test_error_types import ERROR_DIR

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


def _profile_cfg():
    return apply_profile(load_config(CONFIG), DETECTOR_ONLY)


def test_detector_only_profile_is_a_strict_tracked_changes_configuration():
    cfg = _profile_cfg()

    assert cfg.api.model == "gpt-5.6-luna" and cfg.api.effort == "low"
    assert cfg.normalize.quotes is False and cfg.normalize.spaces is False
    assert cfg.comments is False and cfg.query_comments is False
    assert cfg.not_applied_comments is False
    assert cfg.excluded_words_comment is False
    assert cfg.change_log is False and cfg.report_explanations is False
    assert cfg.sweeps == [] and cfg.rounds.count == 1
    assert cfg.edit_guard.enabled is True and cfg.audit == "strict"
    assert not (set(cfg.error_type_keys) & QUERY_ONLY_TYPES)

    disabled = (
        cfg.spellcheck, cfg.consistency, cfg.glossary, cfg.storysheet,
        cfg.continuity, cfg.chapter_continuity, cfg.adjudicate, cfg.rewrite,
        cfg.languagetool, cfg.sapling, cfg.smoothing, cfg.factcheck,
        cfg.residuals, cfg.meaning_check, cfg.fix_check,
    )
    assert all(stage.enabled is False for stage in disabled)
    assert cfg.low_confidence.confirm is False
    assert cfg.ensemble.detectors == []
    assert cfg.ensemble.verifier_model is None
    assert cfg.examination_graph.enabled is True
    assert cfg.examination_graph.production_verdicts is True
    assert cfg.examination_graph.spell_sites is False
    assert cfg.examination_graph.judgment.enabled is False

    # The exact object stored by a batch manifest remains valid on reload.
    restored = Config.model_validate(cfg.model_dump(mode="json"))
    assert restored.model_dump(mode="json") == cfg.model_dump(mode="json")


def test_cli_profile_allows_an_explicit_detector_model_override():
    cfg, _ = _configure(Namespace(
        config=str(CONFIG), profile=DETECTOR_ONLY, model="gpt-5.6-sol"))
    assert cfg.api.model == "gpt-5.6-sol"
    assert cfg.api.effort == "low"
    assert cfg.comments is False and cfg.audit == "strict"


def test_detector_only_profile_does_not_bypass_phase_two_kill_switch(monkeypatch):
    monkeypatch.setenv("DOCPROOF_EXAMINATION_GRAPH", "0")
    cfg = _profile_cfg()
    assert cfg.examination_graph.enabled is False
    assert cfg.examination_graph.production_verdicts is False


def test_batch_manifest_freezes_profile_and_requests_phase_two_receipts(tmp_path):
    cfg = _profile_cfg()
    provider = ScriptedBatchProvider()
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path)

    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    assert job.request_count == prepared.request_count
    frozen = Config.model_validate(batchlib.load(tmp_path, job.job_id).config)
    assert frozen.model_dump(mode="json") == cfg.model_dump(mode="json")
    assert all("reviewed_paragraph_ids" in request.schema["properties"]
               for request in provider.calls[0]["requests"])


def test_mock_review_contains_revisions_without_comment_anchors(tmp_path):
    mocks = tmp_path / "mocks.json"
    mocks.write_text(json.dumps([{
        "para_id": "body-0000",
        "error_type": "comma_splice",
        "original_text": "The manuscript was finished, nobody wanted to read it.",
        "corrected_text": "The manuscript was finished; nobody wanted to read it.",
        "confidence": "high",
    }]), encoding="utf-8")

    rc = main(["review", str(FIXTURES / "simple.docx"),
               "--profile", DETECTOR_ONLY, "--mock-findings", str(mocks),
               "--out", str(tmp_path)])
    assert rc == 0
    reviewed = tmp_path / "simple - Atmosphere Press Proofreader.docx"
    with ZipFile(reviewed) as package:
        document = package.read("word/document.xml")
    assert b"<w:del" in document and b"<w:ins" in document
    assert b"<w:commentRangeStart" not in document
    assert b"<w:commentReference" not in document
    assert not list(tmp_path.glob("*Change Log.docx"))
