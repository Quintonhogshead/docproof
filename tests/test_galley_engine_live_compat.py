"""Preserve the installed worker's audited packaging and budget closeout paths."""
import json
import zipfile

import pytest

from galley import verify
from galley.engine_phases import EnginePhaseError, EnginePhases
from tests.test_galley_engine_phases import _driver, _complete_verification


pytestmark = pytest.mark.skipif(
    not hasattr(verify, "reading_input_snapshot")
    or not hasattr(EnginePhases, "_exhausted_review_budget"),
    reason="Requires the installed worker's reading-input and closeout features")


def _proven(tmp_path, monkeypatch):
    phases = EnginePhases(_driver(tmp_path), lambda spec: pytest.fail("No new read is authorized"))
    run = _complete_verification(phases, monkeypatch)
    phases.verify()
    return phases, run


def _exhaust(phases, monkeypatch):
    from docproof.resource_ledger import append_usage
    from galley.manifest import sha256_file
    monkeypatch.setenv("DOCPROOF_RESOURCE_GROUP", "review")
    monkeypatch.setenv("DOCPROOF_RESOURCE_MAX_CALLS", "1")
    path = phases.directory.parent / "resources.jsonl"
    args = dict(path=path, receipt_id="completed-call", operation_id="completed-read",
                source_sha256=sha256_file(phases.driver.book),
                config_sha256=sha256_file(phases._config()), model="test", transport="fake")
    append_usage(**args, status="started")
    append_usage(**args, usage={"input_tokens": 100, "output_tokens": 5})
    assert phases._exhausted_review_budget()
    return path


def test_recorded_packaging_transition_reuses_and_restores_exact_reader_proofs(tmp_path, monkeypatch):
    phases, run = _proven(tmp_path, monkeypatch)
    before = verify.reading_input_snapshot(run)
    initial = (phases.directory / "initial-coverage.json").read_bytes()
    artifact = run / "verification/type-compare/finished_walk.json"
    proof = artifact.read_bytes()
    with zipfile.ZipFile(verify.deliverable_docx(run), "a") as package:
        package.comment = b"Audited packaging-only update"
    assert not phases._coverage(run, require_full_passes=True)
    verify.record_reading_input_transition(run, before, reason="Audited packaging-only update")
    assert phases._coverage(run, require_full_passes=True)
    phases.verify()
    assert (phases.directory / "initial-coverage.json").read_bytes() == initial
    artifact.unlink()
    phases.verify()
    assert artifact.read_bytes() == proof
    assert phases._coverage(run, require_full_passes=True)


def test_exhausted_review_budget_can_restore_proven_outputs_without_new_calls(tmp_path, monkeypatch):
    phases, run = _proven(tmp_path, monkeypatch)
    ledger = _exhaust(phases, monkeypatch)
    original_ledger = ledger.read_bytes()
    artifact = run / "finished_walk.json"
    original_artifact = artifact.read_bytes()
    artifact.unlink()
    phases.verify()
    assert artifact.read_bytes() == original_artifact
    assert ledger.read_bytes() == original_ledger


def test_exhausted_review_budget_never_rebuys_a_missing_response_window(tmp_path, monkeypatch):
    phases, run = _proven(tmp_path, monkeypatch)
    ledger = _exhaust(phases, monkeypatch)
    original_ledger = ledger.read_bytes()
    artifact = json.loads((run / "finished_walk.json").read_text())
    proof = artifact["verification_provenance"]
    window = next(iter(proof["windows"]))
    (run / verify._CHECKPOINT_DIR / proof["scope"] / proof["invocation_id"] / (window + ".json")).unlink()
    with pytest.raises(EnginePhaseError, match="Review budget exhausted") as failure:
        phases.verify()
    assert not failure.value.retryable and failure.value.kind == "limit"
    assert ledger.read_bytes() == original_ledger


def test_exhausted_review_closeout_remains_observational_and_uses_actual_budget(tmp_path, monkeypatch):
    phases, run = _proven(tmp_path, monkeypatch)
    ledger = _exhaust(phases, monkeypatch)
    original_ledger = ledger.read_bytes()
    (run / "editmap.json").write_text('{}')
    advanced = []
    monkeypatch.setattr(phases, "_advance", advanced.append)
    phases.settle()
    settlement = json.loads((run / "settlement.json").read_text())
    closeout = settlement["convergence"]["resource_budget_closeout"]
    assert settlement["rounds"] == 0
    assert settlement["records"] == []
    assert closeout["binding"]["review_budget"]["calls"] == 1
    assert closeout["binding"]["review_budget"]["max_calls"] == 1
    assert settlement["cost"]["new_api_calls"] == 0
    assert advanced == ["settled"]
    assert ledger.read_bytes() == original_ledger
