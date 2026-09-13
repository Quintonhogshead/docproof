"""Consolidation precedes the corrected-text read, including after a crash."""
import json
import os

import pytest

from docproof.__main__ import main
from galley import driver as gd
from galley.state_machine import RunStateMachine
from .test_driver import FIXTURE, FakeSpawner, _driver


def test_both_workflows_consolidate_all_completed_lanes_before_verify():
    for mechanical in (True, False):
        phases = gd.select_phases(mechanical_only=mechanical)
        assert phases.index("audit") < phases.index("adjudicate")
        assert phases.index("adjudicate") + 1 == phases.index("verify")
        if not mechanical:
            assert phases.index("reread") < phases.index("adjudicate")


def test_audit_alone_cannot_complete_adjudication(tmp_path):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    d = _driver(book, tmp_path, only_phases=["audit", "adjudicate", "verify"])
    ws = gd.seed_workspace(book, d.slug, workspace_root=d.workspace_root)
    spawn = FakeSpawner(ws, skip_state="adjudicate")
    d.spawn = spawn
    result = d.run()
    assert spawn.phases == ["audit", "adjudicate"]
    assert result.stopped_at == "adjudicate"
    assert "adjudicated" in result.reason


def _evidence(tmp_path):
    ws = tmp_path / "workspace"
    final = ws / "runs" / "final"
    final.mkdir(parents=True)
    (final / "findings.json").write_text('{"findings": []}')
    (final / "Book - Proofread.docx").write_bytes(FIXTURE.read_bytes())
    (ws / "runs" / "ADJUDICATE.md").write_text(
        "Ladder and Sonnet rows included; bespoke duplicate recorded. Final: runs/final.")
    machine = RunStateMachine()
    machine.advance("audited")
    machine.save(ws / "state.json")
    return ws, final


def test_state_command_pins_the_curated_build_not_a_newer_ladder(tmp_path):
    ws, final = _evidence(tmp_path)
    assert main(["galley", "state", str(ws), "--advance", "adjudicated",
                 "--results", str(final)]) == 0
    assert RunStateMachine.load(ws / "state.json").current == "adjudicated"
    ladder = ws / "runs" / "ladder"
    ladder.mkdir()
    (ladder / "findings.json").write_text('{"findings": []}')
    os.utime(ladder / "findings.json", (4_000_000_000, 4_000_000_000))
    (tmp_path / "Book.docx").write_bytes(FIXTURE.read_bytes())
    d = gd.Driver(book=tmp_path / "Book.docx", slug="workspace", workspace_root=tmp_path)
    assert d._final_run() == final
    # Legitimate settlement changes must not invalidate the adjudication state.
    (final / "findings.json").write_text('{"findings": [{"settled": true}]}')
    from galley.state_machine import hash_artifact
    assert RunStateMachine.load(ws / "state.json").verify_resume(
        artifact_hasher=hash_artifact) == []


@pytest.mark.parametrize("missing", ["results", "findings", "manuscript", "report"])
def test_adjudication_requires_reviewable_evidence(tmp_path, missing):
    ws, final = _evidence(tmp_path)
    files = {"findings": final / "findings.json",
             "manuscript": final / "Book - Proofread.docx",
             "report": ws / "runs" / "ADJUDICATE.md"}
    if missing in files:
        files[missing].unlink()
    args = ["galley", "state", str(ws), "--advance", "adjudicated"]
    if missing != "results":
        args += ["--results", str(final)]
    assert main(args) == 2
    assert RunStateMachine.load(ws / "state.json").current == "audited"
    assert not (ws / "runs/driver/final-run.json").exists()


def test_phase_contract_accounts_for_both_paid_and_subscription_findings():
    prompt = gd.phase_prompt("adjudicate", "Book.docx")
    for evidence in ("plan ledger", "bespoke sweep", "Sonnet", "row count",
                     "excluded with a reason", "No completed lane", "final-replay"):
        assert evidence in prompt
    assert "corrected document, never an earlier ladder build" in gd.phase_prompt("verify", "Book.docx")


def test_settlement_moves_the_pin_to_its_corrected_rebuild(tmp_path):
    ws, first = _evidence(tmp_path)
    assert main(["galley", "state", str(ws), "--advance", "adjudicated",
                 "--results", str(first)]) == 0
    rebuilt = ws / "runs" / "final2"
    rebuilt.mkdir()
    (rebuilt / "findings.json").write_text('{"findings": []}')
    (rebuilt / "Book - Proofread.docx").write_bytes(FIXTURE.read_bytes())
    assert main(["galley", "state", str(ws), "--advance", "settled",
                 "--results", str(rebuilt)]) == 0
    os.utime(first / "findings.json", (4_000_000_000, 4_000_000_000))
    (tmp_path / "Book.docx").write_bytes(FIXTURE.read_bytes())
    d = gd.Driver(book=tmp_path / "Book.docx", slug="workspace", workspace_root=tmp_path)
    assert d._final_run() == rebuilt
    pin = json.loads((ws / "runs/driver/final-run.json").read_text())
    assert pin["selected_by"] == "settle"


def test_build_selection_is_durable_before_completion_state(tmp_path, monkeypatch):
    ws, final = _evidence(tmp_path)
    def interrupted_save(self, path):
        pin = json.loads((ws / "runs/driver/final-run.json").read_text())
        assert pin["run"] == "runs/final"
        raise OSError("interrupted before state commit")
    monkeypatch.setattr(RunStateMachine, "save", interrupted_save)
    with pytest.raises(OSError, match="state commit"):
        main(["galley", "state", str(ws), "--advance", "adjudicated",
              "--results", str(final)])
    assert RunStateMachine.load(ws / "state.json").current == "audited"


def test_code_execution_still_uses_editorial_session_for_adjudication(tmp_path, monkeypatch):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    d = _driver(book, tmp_path, execution_mode="code", astra_review=True,
                only_phases=["audit", "adjudicate", "verify"])
    calls = []
    monkeypatch.setattr(d, "_run_code_phase", lambda phase, env, result:
                        calls.append(("code", phase)))
    monkeypatch.setattr(d, "_run_session_phase", lambda phase, env, result:
                        calls.append(("session", phase)))
    d.run()
    assert calls == [("code", "audit"), ("session", "adjudicate"), ("code", "verify")]
