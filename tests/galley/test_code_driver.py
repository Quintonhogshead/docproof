"""Exercise code scheduling through real CLI verbs with offline readers."""
import json
import shutil

import pytest

import docproof.__main__ as cli
from docproof.resource_ledger import use_context
from galley.driver import Driver, PhaseResult
from galley.engine_phases import EnginePhases
from tests.galley.test_settle import _build, _manuscript, _replay_config
from tests.galley.test_verify_recovery import Provider


@pytest.fixture
def code_job(tmp_path, monkeypatch):
    from docproof.providers import subagent
    from galley import verify
    monkeypatch.setattr(verify, "UNREAD", [])
    monkeypatch.setattr(verify, "UNREAD_BATCHES", [])
    monkeypatch.setattr(verify, "_LOSSES", [])
    workspace = tmp_path / "books" / "fixture"
    workspace.mkdir(parents=True)
    src = _manuscript(tmp_path, ["The book was quiet.", "The story ends here."])
    run = _build(workspace, src, [], out="runs/final")
    cfg = workspace / "runs" / "mech.yaml"
    shutil.copyfile(_replay_config(workspace), cfg)
    assert cli.main(["galley", "approve", str(src), "--config", str(cfg),
                     "--budget", "10", "--mechanical-only", "--out",
                     str(workspace / "approval.json")]) == 0
    readers = []
    class OfflineReader(Provider):
        name = "subagent"
        def __init__(self, model=None, **kwargs):
            super().__init__()
            self.model = subagent.resolve_model(model)
            self.effort = kwargs.get("effort")
            self.max_turns = subagent.MAX_TURNS
            readers.append(self)
    monkeypatch.setattr(subagent, "SubagentProvider", OfflineReader)
    commands = []
    def command(spec):
        commands.append(spec)
        with use_context(spec.env):
            rc = cli.main(spec.argv[1:])
        return PhaseResult(spec.phase, rc, spec.log_path)
    driver = Driver(src, "fixture", workspace_root=tmp_path / "books",
                    execution_mode="code", only_phases=["verify", "settle"],
                    command_spawn=command, log=lambda _: None,
                    env={"CLAUDE_CODE_OAUTH_TOKEN": "offline-test-token"})
    return driver, commands, readers, run


def test_code_driver_keeps_two_reads_and_clean_closeout_buys_no_extra_read(code_job):
    driver, commands, readers, run = code_job
    result = driver.run()
    assert result.outcome != "blocked", result.reason
    assert [spec.argv[2] for spec in commands] == ["verify", "verify", "settle"]
    assert [spec.argv[spec.argv.index("--verification-pass") + 1]
            for spec in commands[:2]] == ["primary", "type-compare"]
    assert sum(len(reader.calls) for reader in readers) == 2
    assert json.loads((run / "settlement.json").read_text())["open"] == []
    assert (driver.workspace / "runs/driver/engine/initial-coverage.json").exists()
    assert all(spec.env["DOCPROOF_RESOURCE_GROUP"] == "review" for spec in commands)
    # A restart at completed settlement validates saved evidence without new calls.
    driver.only_phases = ["settle"]
    resumed = driver.run()
    assert resumed.outcome != "blocked", resumed.reason
    assert len(commands) == 3


def test_code_driver_wont_turn_incomplete_read_into_editorial_handoff(code_job, monkeypatch):
    driver, commands, readers, run = code_job
    from docproof.providers.base import ProviderResult
    monkeypatch.setattr(Provider, "complete_structured", lambda *a, **kw:
                        ProviderResult(parsed=None, stop_reason="max_tokens"))
    result = driver.run()
    assert result.outcome == "blocked"
    assert result.stopped_at == "verify"
    assert not result.handoff
    assert not (run / "outcome.json").exists()
    assert not (driver.workspace / "runs/outcome.json").exists()
    assert len(commands) == 1


def test_completed_settlement_requires_original_read_receipts_on_resume(code_job):
    driver, commands, readers, run = code_job
    result = driver.run()
    assert result.outcome != "blocked", result.reason
    (driver.workspace / "runs/driver/engine/initial-coverage.json").unlink()
    driver.only_phases = ["settle"]
    result = driver.run()
    assert result.outcome == "blocked"
    assert len(commands) == 3


def test_unversioned_existing_build_keeps_legacy_mode_on_upgrade(code_job):
    driver, _, _, _ = code_job
    driver.execution_mode = None
    assert driver.resolve_execution_mode() == "session"


def test_resuming_versioned_build_keeps_saved_code_mode(code_job):
    driver, _, _, _ = code_job
    path = driver.workspace / "runs/driver/driver.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"execution_mode": "code"}))
    driver.execution_mode = None
    assert driver.resolve_execution_mode() == "code"


def test_new_book_defaults_to_code_orchestration(tmp_path):
    driver = Driver(tmp_path / "source.docx", "new", workspace_root=tmp_path,
                    execution_mode=None)
    assert driver.resolve_execution_mode() == "code"


def test_approval_and_audit_use_direct_commands_with_one_configured_audit_read(code_job, monkeypatch):
    from galley.driver import seed_workspace, DriveResult
    driver, commands, _, run = code_job
    seed_workspace(driver.book, driver.slug, workspace_root=driver.workspace_root)
    (driver.workspace / "approval.json").unlink()
    audit_reader = Provider()
    monkeypatch.setattr(cli, "build_provider", lambda *a, **kw: audit_reader)
    result = DriveResult(workspace=driver.workspace)
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "offline-test-token"}
    assert driver._run_code_phase("approve", env, result) is None, result.reason
    assert driver._run_code_phase("audit", env, result) is None, result.reason
    assert [spec.argv[2] for spec in commands] == ["approve", "audit"]
    assert len(audit_reader.calls) == 1
    assert (driver.workspace / "runs/audit.json").is_file()
