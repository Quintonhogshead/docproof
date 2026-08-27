"""Approval manifest, model-route visibility, and the delivery certificate
(galley/manifest.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docproof.genre import materialize_genre_pack
from galley.manifest import (build_manifest, certify_run, config_hash,
                             model_routes, providers_in_use, verify_plan)

CONFIG = Path(__file__).parent.parent / "config" / "default.yaml"


def _mech_cfg():
    cfg, _ = materialize_genre_pack(CONFIG, "general_fiction",
                                    stage="mechanical-wave")
    return cfg


def _source(tmp_path) -> Path:
    p = tmp_path / "book.docx"
    p.write_bytes(b"fake manuscript bytes")
    return p


# --- routes ------------------------------------------------------------------

def test_model_routes_report_active_ensemble_and_provider():
    routes = model_routes(_mech_cfg())
    active = {r.role: r for r in routes if r.active}
    assert "ensemble.detector[gpt-5.6-luna]" in active
    assert active["ensemble.detector[gpt-5.6-luna]"].provider == "openai"
    assert any(r.role == "ensemble.verifier" and r.active for r in routes)


def test_providers_in_use_is_the_active_provider_set():
    assert set(providers_in_use(_mech_cfg())) == {"anthropic", "openai"}


def test_disabled_lane_routes_are_marked_inactive():
    routes = {r.role: r for r in model_routes(_mech_cfg())}
    # The mechanical-wave stage turns the two gates ON (it IS the "both gates"
    # recipe) -> active routes; factcheck stays off -> inactive route.
    if "meaning_check.model" in routes:
        assert routes["meaning_check.model"].active is True
    if "factcheck.model" in routes:
        assert routes["factcheck.model"].active is False


# --- manifest + verify -------------------------------------------------------

def test_build_manifest_captures_hashes_models_and_lanes(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0, stage="mechanical-wave")
    assert m["source_sha256"] and m["config_sha256"] == config_hash(cfg)
    assert "gpt-5.6-luna" in m["allowed_models"]
    assert set(m["allowed_providers"]) == {"anthropic", "openai"}
    assert "ensemble" in m["enabled_lanes"]
    assert m["max_spend_usd"] == 20.0


def test_verify_plan_clean_run_has_no_deviations(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0)
    assert verify_plan(m, source=src, cfg=cfg) == []


def test_verify_plan_flags_a_changed_manuscript(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0)
    src.write_bytes(b"a different manuscript")
    kinds = {d.kind for d in verify_plan(m, source=src, cfg=cfg)}
    assert "source_changed" in kinds


def test_verify_plan_flags_a_model_outside_the_approved_set(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0)
    cfg.api.model = "claude-opus-5"
    kinds = {d.kind for d in verify_plan(m, source=src, cfg=cfg)}
    assert "model_not_approved" in kinds


def test_verify_plan_flags_budget_over_cap(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0)
    kinds = {d.kind for d in verify_plan(m, source=src, cfg=cfg,
                                         budget_usd=25.0)}
    assert "budget_over_cap" in kinds


# --- certify -----------------------------------------------------------------

def _run_dir(tmp_path, envelope: dict) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps(envelope))
    return run


def test_certify_passes_a_clean_run(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=20.0)
    run = _run_dir(tmp_path, {"findings": [{"a": 1}],
                              "cost": {"total_usd": 4.2},
                              "checkpoint": {"x": 1}})
    cert = certify_run(run, manifest=m, cfg=cfg, source=src)
    assert cert.passed
    assert {c.name for c in cert.checks} >= {
        "source hash", "config hash", "approved model routes",
        "budget within approval", "artifact scan"}


def test_certify_fails_zero_cost_anomaly(tmp_path):
    run = _run_dir(tmp_path, {"findings": [{"a": 1}, {"b": 2}],
                              "cost": {"total_usd": 0.0}})
    cert = certify_run(run)
    anomaly = [c for c in cert.checks if c.name == "zero-cost anomaly"]
    assert anomaly and anomaly[0].status == "fail"
    assert not cert.passed


def test_certify_fails_budget_over_cap(tmp_path):
    cfg = _mech_cfg()
    src = _source(tmp_path)
    m = build_manifest(source=src, config_path=str(CONFIG), cfg=cfg,
                       max_spend_usd=3.0)
    run = _run_dir(tmp_path, {"findings": [{"a": 1}],
                              "cost": {"total_usd": 4.2},
                              "checkpoint": {"x": 1}})
    cert = certify_run(run, manifest=m, cfg=cfg, source=src)
    budget = [c for c in cert.checks if c.name == "budget within approval"]
    assert budget and budget[0].status == "fail"


def test_certify_fails_paid_run_missing_checkpoint(tmp_path):
    run = _run_dir(tmp_path, {"findings": [{"a": 1}],
                              "cost": {"total_usd": 4.2}})  # no checkpoint
    cert = certify_run(run)
    ckpt = [c for c in cert.checks if c.name == "checkpoint present"]
    assert ckpt and ckpt[0].status == "fail"


def test_certify_flags_a_merge_artifact_in_corrected_text(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps(
        {"findings": [{"finding_id": "f-0001", "para_id": "body-0001",
                       "original_text": "She paused then left.",
                       "corrected_text": "She paused,, then left."}],
         "cost": {"total_usd": 0.0}}))
    cert = certify_run(run)
    scan = [c for c in cert.checks if c.name == "artifact scan"]
    assert scan and scan[0].status == "fail"


def test_certify_does_not_flag_artifacts_quoted_from_the_original(tmp_path):
    """The change log and findings faithfully quote pre-fix text; the artifacts
    IN those quotes are what the run fixed, not defects of the deliverable."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps(
        {"findings": [{"finding_id": "f-0001", "para_id": "body-0001",
                       "original_text": "She paused,,  then left. ",
                       "corrected_text": "She paused, then left."}],
         "cost": {"total_usd": 0.0}}))
    (run / "change_log.md").write_text("> She paused,,  then left. ")
    cert = certify_run(run)
    scan = [c for c in cert.checks if c.name == "artifact scan"]
    assert scan and scan[0].status == "pass"


def test_import_judgments_run_is_not_a_zero_cost_anomaly(tmp_path):
    """A model-free import legitimately costs $0 with findings — it must not
    trip the zero-cost anomaly check."""
    run = _run_dir(tmp_path, {"findings": [{"a": 1}],
                              "cost": {"total_usd": 0.0},
                              "judge_model": "external:import-judgments"})
    cert = certify_run(run)
    anomaly = [c for c in cert.checks if c.name == "zero-cost anomaly"]
    assert anomaly and anomaly[0].status == "pass"


# --- delivered text hygiene ---------------------------------------------------

def _docx_in(run, name, *texts):
    import docx as _docx
    d = _docx.Document()
    for t in texts:
        d.add_paragraph(t)
    d.save(run / name)


def test_certify_text_hygiene_skips_without_a_docx(tmp_path):
    run = _run_dir(tmp_path, {"findings": [], "cost": {"total_usd": 0.0}})
    cert = certify_run(run)
    check = next(c for c in cert.checks if c.name == "delivered text hygiene")
    assert check.status == "skip"


def test_certify_text_hygiene_fails_on_double_spaces(tmp_path):
    run = _run_dir(tmp_path, {"findings": [], "cost": {"total_usd": 0.0}})
    _docx_in(run, "book - Atmosphere Press Proofreader.docx",
             "A clean paragraph.", "Two  spaces survived here.",
             "Trailing spaces here.  ")
    cert = certify_run(run)
    check = next(c for c in cert.checks if c.name == "delivered text hygiene")
    assert check.status == "fail"
    assert "double space" in check.detail


def test_certify_text_hygiene_passes_clean_text_and_dividers(tmp_path):
    run = _run_dir(tmp_path, {"findings": [], "cost": {"total_usd": 0.0}})
    _docx_in(run, "book.docx", "A clean paragraph.", "*   *   *", "Another.")
    cert = certify_run(run)
    check = next(c for c in cert.checks if c.name == "delivered text hygiene")
    assert check.status == "pass"
