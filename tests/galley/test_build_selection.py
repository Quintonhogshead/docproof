"""Recover final-build pointers without falling back to an old manuscript."""
import json

import pytest

from galley.build_selection import BuildSelectionError, final_run
from galley.manifest import sha256_file
from .test_driver import FIXTURE


def _run(ws, name):
    path = ws / "runs" / name
    path.mkdir(parents=True)
    (path / "findings.json").write_text('{"findings": []}')
    (path / "Book.docx").write_bytes(FIXTURE.read_bytes())
    return path


def _write(ws, name, value):
    path = ws / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_latest_final_state_overrides_a_valid_older_pin(tmp_path):
    _run(tmp_path, "old")
    corrected = _run(tmp_path, "corrected")
    _write(tmp_path, "runs/driver/final-run.json", {"run": "runs/old", "source_sha256": "source"})
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": [
        {"state": "adjudicated", "results_run": "runs/old"},
        {"state": "settled", "results_run": "runs/corrected"}]})
    assert final_run(tmp_path, "source") == corrected


@pytest.mark.parametrize("state", ["adjudicated", "settled"])
def test_later_settlement_pin_survives_interrupted_state_save(tmp_path, state):
    _run(tmp_path, "old")
    corrected = _run(tmp_path, "corrected")
    selected = {"run": "runs/corrected", "source_sha256": "source",
                "selected_by": "settle", "at": "2026-09-12T10:01:00+00:00"}
    _write(tmp_path, "runs/driver/final-run.json", selected)
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": [
        {"state": state, "results_run": "runs/old", "at": "2026-09-12T10:00:00+00:00"}]})
    assert final_run(tmp_path, "source") == corrected
    assert json.loads((tmp_path / "runs/driver/final-run.json").read_text()) == selected


def test_cli_settlement_state_save_crash_resumes_the_new_corrected_build(tmp_path, monkeypatch):
    from docproof.__main__ import main
    from galley.driver import Driver
    from galley.state_machine import RunStateMachine

    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = tmp_path / "workspace"
    _run(ws, "old")
    corrected = _run(ws, "corrected")
    machine = RunStateMachine(source_sha256=sha256_file(book))
    machine.advance("adjudicated", results_run="runs/old")
    machine.save(ws / "state.json")
    def interrupted_save(self, path):
        raise OSError("interrupted before state commit")
    monkeypatch.setattr(RunStateMachine, "save", interrupted_save)
    with pytest.raises(OSError, match="state commit"):
        main(["galley", "state", str(ws), "--advance", "settled",
              "--results", str(corrected)])
    assert RunStateMachine.load(ws / "state.json").current == "adjudicated"
    driver = Driver(book=book, slug="workspace", workspace_root=tmp_path)
    assert driver._final_run() == corrected
    assert json.loads((ws / "runs/driver/final-run.json").read_text())["selected_by"] == "settle"


@pytest.mark.parametrize("failure", ["older", "unknown_time", "backward_phase", "malformed_selector", "invalid_findings", "missing_manuscript"])
def test_pending_selection_requires_complete_forward_transaction(tmp_path, failure):
    old = _run(tmp_path, "old")
    corrected = _run(tmp_path, "corrected")
    selected = {"run": "runs/corrected", "source_sha256": "source",
                "selected_by": "settle", "at": "2026-09-12T10:01:00+00:00"}
    state = "adjudicated"
    if failure == "older":
        selected["at"] = "2026-09-12T09:00:00+00:00"
    elif failure == "unknown_time":
        selected["at"] = "unknown"
    elif failure == "backward_phase":
        state = "certified"
    elif failure == "malformed_selector":
        selected["selected_by"] = {"state": "settle"}
    elif failure == "invalid_findings":
        (corrected / "findings.json").write_text('{"findings": {}}')
    else:
        (corrected / "Book.docx").unlink()
    _write(tmp_path, "runs/driver/final-run.json", selected)
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": [
        {"state": state, "results_run": "runs/old", "at": "2026-09-12T10:00:00+00:00"}]})
    if failure in ("invalid_findings", "missing_manuscript"):
        with pytest.raises(BuildSelectionError, match="Pending selected"):
            final_run(tmp_path, "source")
    else:
        assert final_run(tmp_path, "source") == old


@pytest.mark.parametrize("failure", ["stale_package", "missing_loop_target", "malformed_history", "malformed_state", "malformed_loop"])
def test_other_valid_source_bound_evidence_recovers_damaged_metadata(tmp_path, failure):
    corrected = _run(tmp_path, "corrected")
    _run(tmp_path, "old")
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": []})
    _write(tmp_path, "runs/driver/engine/initial-coverage.json", {"source": "source", "run": str(corrected)})
    if failure == "stale_package":
        _write(tmp_path, "runs/driver/package.json", {"run": "runs/corrected", "build_sha256": "old bytes"})
    elif failure == "missing_loop_target":
        _write(tmp_path, "runs/driver/engine/review-loop.json", {"identity": {"source": "source", "run": "runs/gone"}})
    elif failure == "malformed_history":
        _write(tmp_path, "state.json", {"source_sha256": "source", "history": None})
    elif failure == "malformed_state":
        _write(tmp_path, "state.json", {"source_sha256": "source", "history": [{"state": {"bad": "record"}}]})
    else:
        _write(tmp_path, "runs/driver/engine/review-loop.json", {"identity": ["bad record"]})
    assert final_run(tmp_path, "source") == corrected


def test_conflicting_valid_final_receipts_do_not_choose_by_age(tmp_path):
    first = _run(tmp_path, "first")
    second = _run(tmp_path, "second")
    _write(tmp_path, "runs/driver/engine/initial-coverage.json", {"source": "source", "run": str(first)})
    _write(tmp_path, "runs/driver/engine/review-loop.json", {"identity": {"source": "source", "run": str(second)}})
    with pytest.raises(BuildSelectionError, match="conflicting"):
        final_run(tmp_path, "source")


def test_missing_latest_state_cannot_fall_back_to_valid_package(tmp_path):
    old = _run(tmp_path, "old")
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": [
        {"state": "settled", "results_run": "runs/corrected"}]})
    _write(tmp_path, "runs/driver/package.json", {"run": "runs/old", "build_sha256": sha256_file(old / "Book.docx")})
    with pytest.raises(BuildSelectionError, match="Recorded corrected manuscript is missing"):
        final_run(tmp_path, "source")


def test_audit_state_does_not_pin_an_earlier_build(tmp_path):
    _run(tmp_path, "old")
    corrected = _run(tmp_path, "corrected")
    _write(tmp_path, "state.json", {"source_sha256": "source", "history": [
        {"state": "audited", "results_run": "runs/old"}]})
    _write(tmp_path, "runs/driver/engine/initial-coverage.json", {"source": "source", "run": str(corrected)})
    assert final_run(tmp_path, "source") == corrected
