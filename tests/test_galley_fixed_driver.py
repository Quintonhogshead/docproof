"""Fixed orchestration is selected and resumed without launching a Brain."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from docproof.__main__ import main
from galley import agent, driver as gd


@pytest.fixture
def book(tmp_path):
    source = tmp_path / "Test - Book 1.docx"
    source.write_bytes((Path(__file__).parent / "fixtures/tiny_novel.docx").read_bytes())
    return source


@pytest.fixture
def fixed_module(monkeypatch):
    module = types.ModuleType("galley.fixed_workflow")
    module.workflow_plan = lambda: [
        {"stage": "intake", "model": "code", "description": "Preserve source."},
        {"stage": "poetry", "model": "claude-sonnet-4-6", "description": "Classify samples."},
        {"stage": "astra", "model": "gpt-6-astra", "description": "Final review."},
    ]
    module.run_fixed_driver = lambda driver: gd.DriveResult(workspace=driver.workspace)
    monkeypatch.setitem(sys.modules, "galley.fixed_workflow", module)
    return module


def test_new_cli_jobs_choose_fixed_without_legacy_setup(book, tmp_path, monkeypatch, fixed_module, capsys):
    calls = []

    def fixed(driver):
        assert (driver.workspace / "source" / book.name).read_bytes() == book.read_bytes()
        assert driver.execution_mode == "fixed"
        calls.append(driver)
        return gd.DriveResult(workspace=driver.workspace)

    monkeypatch.setattr(fixed_module, "run_fixed_driver", fixed)
    monkeypatch.setattr(gd, "build_env", lambda *a, **kw: pytest.fail("legacy environment"))
    monkeypatch.setattr(gd, "spawn_claude", lambda *a, **kw: pytest.fail("supervising Brain"))
    rc = main(["galley", "drive", "--book", str(book), "--slug", "test",
               "--workspace-root", str(tmp_path / "work"), "--json"])
    assert rc == 0 and len(calls) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "done"


def test_direct_driver_retains_session_default(book, tmp_path):
    driver = gd.Driver(book, "test", workspace_root=tmp_path)
    assert driver.resolve_execution_mode() == "session"


@pytest.mark.parametrize("mode", ["session", "code", "fixed"])
def test_saved_execution_mode_survives_upgrade(book, tmp_path, mode):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode=None)
    saved = driver.workspace / "runs/driver/driver.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"execution_mode": mode}))
    assert driver.resolve_execution_mode() == mode


def test_fixed_checkpoint_pins_mode_before_first_driver_result(book, tmp_path):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode=None)
    marker = driver.workspace / "runs/fixed/workflow.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{}')
    assert driver.resolve_execution_mode() == "fixed"


def test_fixed_checkpoint_cannot_be_downgraded_to_legacy(book, tmp_path):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode="code")
    marker = driver.workspace / "runs/fixed/workflow.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{}')
    with pytest.raises(gd.DriverError, match="uses the fixed workflow"):
        driver.resolve_execution_mode()


@pytest.mark.parametrize("payload", ["broken json", "[]", '{"execution_mode":"unknown"}'])
def test_unreadable_execution_record_never_starts_a_new_recipe(book, tmp_path, payload):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode=None)
    saved = driver.workspace / "runs/driver/driver.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(payload)
    with pytest.raises(gd.DriverError):
        driver.resolve_execution_mode()


@pytest.mark.parametrize("evidence", ["state", "driver", "build", "approval"])
def test_unversioned_progress_stays_legacy_and_refuses_fixed_migration(book, tmp_path, evidence):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode=None)
    ws = gd.seed_workspace(book, "test", workspace_root=tmp_path)
    if evidence == "state":
        from galley.state_machine import RunStateMachine
        state = RunStateMachine.load(ws / "state.json")
        state.advance("intake", by="legacy profile")
        state.save(ws / "state.json")
    else:
        paths = {"driver": "runs/driver/profile.stream.jsonl",
                 "build": "runs/first/findings.json", "approval": "approval.json"}
        path = ws / paths[evidence]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    assert driver.resolve_execution_mode() == "session"
    driver.execution_mode = "fixed"
    with pytest.raises(gd.DriverError, match="mid-book migration"):
        driver.resolve_execution_mode()


@pytest.mark.parametrize("mode", ["session", "code"])
def test_explicit_fixed_cannot_replace_saved_legacy_mode(book, tmp_path, mode):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode="fixed")
    saved = driver.workspace / "runs/driver/driver.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"execution_mode": mode}))
    with pytest.raises(gd.DriverError, match="mid-book migration"):
        driver.resolve_execution_mode()


@pytest.mark.parametrize("kwargs, message", [
    ({"mechanical_only": False}, "mechanical proofreading"),
    ({"astra_review": False}, "final Astra review"),
    ({"start_phase": "verify"}, "completed calls are reused"),
    ({"only_phases": ["poetry"]}, "completed calls are reused"),
    ({"model": "different"}, "pins its reader models"),
    ({"model_by_phase": {"poetry": "different"}}, "pins its reader models"),
    ({"effort": "low"}, "pins its reader models"),
    ({"effort_by_phase": {"poetry": "low"}}, "pins its reader models"),
    ({"approve": "manual"}, "--approve auto"),
    ({"astra_transport": "api"}, "only by legacy workflows"),
    ({"astra_chunk_bytes": 10000}, "pins its Astra"),
    ({"astra_max_output_tokens": 9999}, "pins its Astra"),
    ({"review_rounds": 1}, "legacy verify/settle only"),
    ({"review_calls": 1000}, "legacy verify/settle only"),
    ({"review_output_tokens": 3_000_000}, "legacy verify/settle only"),
    ({"budget_usd": float("nan")}, "finite nonnegative"),
])
def test_fixed_rejects_unhonored_options_before_calls(book, tmp_path, monkeypatch, fixed_module, kwargs, message):
    driver = gd.Driver(book, "test", workspace_root=tmp_path, execution_mode="fixed", **kwargs)
    monkeypatch.setattr(fixed_module, "run_fixed_driver", lambda _: pytest.fail("model work started"))
    with pytest.raises(gd.DriverError, match=message):
        driver.run()


def test_fixed_dry_run_reports_actual_recipe_without_calls(book, tmp_path, monkeypatch, fixed_module, capsys):
    monkeypatch.setattr(fixed_module, "run_fixed_driver", lambda _: pytest.fail("dry run executed"))
    monkeypatch.setattr(gd, "build_env", lambda *a, **kw: pytest.fail("dry run checked session login"))
    rc = main(["galley", "drive", "--book", str(book), "--slug", "test",
               "--workspace-root", str(tmp_path / "work"), "--dry-run", "--json"])
    assert rc == 0
    output = capsys.readouterr().out
    payload = json.loads(output.splitlines()[-1])
    assert payload["execution_mode"] == "fixed" and payload["brains"] == {}
    assert payload["recipe"] == fixed_module.workflow_plan()
    assert payload["phases"] == [row["stage"] for row in payload["recipe"]]
    assert "no supervising Brain" in output
    assert not (tmp_path / "work/test/runs/fixed/workflow.json").exists()


def test_agent_fixed_starts_from_original_without_formatting(book, tmp_path, monkeypatch):
    from galley import codex_runner, intake
    calls = []
    monkeypatch.setattr(codex_runner, "check_login", lambda: pytest.fail("ChatGPT login before poetry classification"))
    monkeypatch.setattr(intake, "format_for_proof", lambda *a, **kw: pytest.fail("formatting before poetry"))

    def run(driver):
        assert driver.book == book and driver.execution_mode == "fixed"
        calls.append("fixed")
        return gd.DriveResult(workspace=driver.workspace)

    monkeypatch.setattr(gd.Driver, "run", run)
    agent._run_driver(book=book, slug="test", workspace_root=tmp_path / "work",
                      astra_transport="codex")
    assert calls == ["fixed"]


@pytest.mark.parametrize("marker", ["runs/fixed/workflow.json", "runs/driver/driver.json"])
def test_agent_fixed_resume_does_not_inject_legacy_phase(tmp_path, marker):
    from galley.state_machine import RunStateMachine
    worker = agent.Agent(agent.AgentEnv("https://offline.invalid", "test", "test"), workspace_root=tmp_path)
    workspace = tmp_path / "test"
    checkpoint = workspace / marker
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"execution_mode": "fixed"}))
    state = RunStateMachine()
    state.advance("adjudicated", by="fixed")
    state.save(workspace / "state.json")
    assert worker.resume_phase("test") == ""


def test_agent_reports_fixed_stage_progress_without_legacy_phase_mapping(tmp_path):
    events = []
    worker = agent.Agent(agent.AgentEnv("https://offline.invalid", "test", "test"),
                         workspace_root=tmp_path, heartbeat=events.append)
    worker._on_progress({"event": "phase_start", "phase": "number_sweep", "model": "Sonnet + Luna"})
    assert events[-1]["phase"] == "number_sweep" and events[-1]["model"] == "Sonnet + Luna"


@pytest.mark.parametrize("valid", [True, False])
def test_agent_pending_delivery_uses_fixed_evidence(book, tmp_path, monkeypatch, valid):
    from galley import astra_review, verify
    from galley.manifest import sha256_file
    from galley.state_machine import RunStateMachine

    workspace = gd.seed_workspace(book, "test", workspace_root=tmp_path / "work")
    run = workspace / "runs/final"
    run.mkdir()
    manuscript = run / "reviewed.docx"
    manuscript.write_bytes(book.read_bytes())
    driver_dir = workspace / "runs/driver"
    driver_dir.mkdir()
    package = {"execution_mode": "fixed", "run": str(run), "source_id": "source-id",
               "packet_sha256": "evidence", "build_sha256": sha256_file(manuscript)}
    (driver_dir / "package.json").write_text(json.dumps(package))
    state = RunStateMachine.load(workspace / "state.json")
    state.advance("certified", by="fixed", results_run="runs/final")
    state.save(workspace / "state.json")
    documents = types.ModuleType("galley.fixed_documents")
    validated, published = [], []

    def validate(saved):
        validated.append(saved)
        if not valid:
            raise ValueError("Fixed evidence changed")
        return {"delivery_ready": True, "packet_sha256": "evidence",
                "review": {"editorial_verdict": "ready"}}

    documents.validate_delivery_package = validate
    monkeypatch.setitem(sys.modules, "galley.fixed_documents", documents)
    monkeypatch.setattr(astra_review, "validate_receipt", lambda _: pytest.fail("legacy evidence validator"))
    monkeypatch.setattr(verify, "deliverable_docx", lambda _: manuscript)
    monkeypatch.setattr(gd, "publish_verified_handoff", lambda *a, **kw: published.append(a))
    worker = agent.Agent(agent.AgentEnv("https://offline.invalid", "test", "test"),
                         workspace_root=tmp_path / "work")
    assert worker._retry_verified_publication({"slug": "test"}, "folder", {}) is valid
    assert validated == [package]
    assert bool(published) is valid
    assert RunStateMachine.load(workspace / "state.json").current == ("delivered" if valid else "certified")
