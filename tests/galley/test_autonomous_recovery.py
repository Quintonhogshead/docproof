"""Recovery completes required work without resetting budgets or selecting old text."""
import json

import pytest

from galley import driver as gd
from galley.build_selection import BuildSelectionError, final_run
from galley.manifest import sha256_file
from galley.recovery import RecoveryLedger, progress_fingerprint
from galley.state_machine import RunStateMachine
from .test_driver import FIXTURE, _driver
from .test_unattended import MeteredSpawner


def test_progress_can_continue_beyond_one_recovery_session(tmp_path):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    class Progress(MeteredSpawner):
        def __call__(self, spec):
            if len(self.calls) < 3:
                self.calls.append(spec)
                checkpoint = ws / "runs/ladder/reader-checkpoint.json"
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                checkpoint.write_text(json.dumps({"completed_windows": len(self.calls)}))
                return gd.PhaseResult(spec.phase, 3, spec.log_path,
                                      "Continue remaining windows", num_turns=2)
            return super().__call__(spec)
    spawn = Progress(ws)
    clock = iter([0, 2, 2, 4, 4, 6, 6, 8])
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     max_turns=12, timeout_s=30, clock=lambda: next(clock)).run()
    assert result.outcome == "done", result.reason
    assert [s.max_turns for s in spawn.calls] == [12, 10, 8, 6]
    assert [s.timeout_s for s in spawn.calls] == [30, 28, 26, 24]


def test_progress_retries_share_a_bounded_continuation_and_keep_session_context(tmp_path, monkeypatch):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    encoded = gd.re.sub(r"[^A-Za-z0-9]", "-", str(ws.resolve()))
    transcripts = home / ".claude" / "projects" / encoded
    transcripts.mkdir(parents=True)
    for number in range(1, 4):
        (transcripts / f"sess-{number}.jsonl").write_text("{}\n")

    class Progress(MeteredSpawner):
        def __call__(self, spec):
            if len(self.calls) < 3:
                self.calls.append(spec)
                number = len(self.calls)
                checkpoint = ws / "runs/ladder/reader-checkpoint.json"
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                checkpoint.write_text(json.dumps({"completed_windows": number}))
                return gd.PhaseResult(spec.phase, 3, spec.log_path,
                    "Continue remaining windows", limit="max_turns" if number == 1 else None,
                    num_turns=spec.max_turns if number == 1 else 2,
                    session_id=f"sess-{number}")
            return super().__call__(spec)

    spawn = Progress(ws)
    clock = iter([0, 10, 10, 12, 12, 14, 14, 16])
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     max_turns=20, timeout_s=60, clock=lambda: next(clock)).run()
    assert result.outcome == "done", result.reason
    assert [s.max_turns for s in spawn.calls] == [20, 10, 8, 6]
    assert [s.timeout_s for s in spawn.calls] == [60, 80, 78, 76]
    for number, spec in enumerate(spawn.calls[1:], 1):
        assert spec.argv[spec.argv.index("--resume") + 1] == f"sess-{number}"
        assert spec.env["BASH_MAX_TIMEOUT_MS"] == str(int(spec.timeout_s * 1000))
    budget = json.loads((ws / "runs/driver/execution-budget.json").read_text())
    assert [(g["turns"], g["seconds"]) for g in budget["grants"]] == [(10, 30)]
    assert [r["turns"] for r in budget["attempts"]] == [20, 2, 2, 3]


def test_recovery_stalls_survive_restart_but_real_progress_recovers(tmp_path):
    for expected in [True, True, False]:
        ledger = RecoveryLedger(tmp_path, "source")
        assert ledger.failed("verify", before="same", after="same", reason="missing evidence") is expected
    assert RecoveryLedger(tmp_path, "source").failed("verify", before="same", after="window completed", reason="remaining windows")


def test_new_logs_timestamps_and_proof_ids_do_not_count_as_reading(tmp_path):
    proof = tmp_path / "runs/final/finished_walk.json"
    proof.parent.mkdir(parents=True)
    proof.write_text(json.dumps({"coverage": [1, 2], "at": "old", "verification_pair_id": "old"}))
    old = progress_fingerprint(tmp_path)
    proof.write_text(json.dumps({"coverage": [1, 2], "at": "new", "verification_pair_id": "new"}))
    (proof.parent / "session.log").write_text("Still trying")
    assert progress_fingerprint(tmp_path) == old
    proof.write_text(json.dumps({"coverage": [1, 2, 3]}))
    assert progress_fingerprint(tmp_path) != old


@pytest.mark.parametrize("pin_state", ["missing", "broken", "missing_target"])
def test_lost_pin_recovers_corrected_build_even_with_newer_old_findings(tmp_path, pin_state):
    ws = tmp_path / "workspace"
    corrected = ws / "runs/corrected"
    corrected.mkdir(parents=True)
    (corrected / "findings.json").write_text('{"findings": []}')
    (corrected / "Book.docx").write_bytes(FIXTURE.read_bytes())
    old = ws / "runs/newer-raw-ladder"
    old.mkdir()
    (old / "findings.json").write_text('{}')
    state = RunStateMachine(source_sha256="source")
    state.advance("settled", source_sha256="source", results_run="runs/corrected")
    state.save(ws / "state.json")
    pin = ws / "runs/driver/final-run.json"
    pin.parent.mkdir()
    if pin_state == "broken":
        pin.write_text('{')
    elif pin_state == "missing_target":
        pin.write_text('{"run": "runs/gone"}')
    assert final_run(ws, "source") == corrected
    assert json.loads(pin.read_text())["recovered_from"] == "state"


def test_missing_corrected_build_never_substitutes_old_build(tmp_path):
    raw = tmp_path / "runs/ladder"
    raw.mkdir(parents=True)
    (raw / "findings.json").write_text('{}')
    state = RunStateMachine(source_sha256="source")
    state.advance("settled", source_sha256="source", results_run="runs/corrected")
    state.save(tmp_path / "state.json")
    with pytest.raises(BuildSelectionError, match="Recorded corrected manuscript is missing"):
        final_run(tmp_path, "source")


def test_transport_recovers_and_auth_failure_keeps_its_type(tmp_path):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    d = _driver(book, tmp_path, sleep=lambda _: None)
    calls = []
    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("connection reset")
        return "saved completion"
    assert d._retry_operation("astra_review", operation) == "saved completion"
    assert len(calls) == 3
    def auth():
        raise gd.CredentialsError("token expired")
    with pytest.raises(gd.CredentialsError):
        d._retry_operation("astra_review", auth)


def test_failed_launch_closes_known_zero_resource_receipt(tmp_path):
    from docproof.resource_ledger import summarize
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    def missing(spec):
        raise gd.ProcessNotStartedError("no CLI")
    d = _driver(book, tmp_path, spawn=missing, only_phases=["ladder"])
    with pytest.raises(gd.ProcessNotStartedError):
        d.run()
    resources = summarize(ws / "runs/driver/resources.jsonl")
    assert resources["incomplete_attempts"] == resources["unknown_usage"] == 0
    assert resources["input_tokens"] == resources["output_tokens"] == 0


def test_temporary_session_outage_waits_for_retry_instead_of_new_code(tmp_path):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    calls = []
    def outage(spec):
        calls.append(spec)
        return gd.PhaseResult(spec.phase, 3, spec.log_path,
                              "service unavailable", num_turns=0)
    result = _driver(book, tmp_path, spawn=outage, only_phases=["ladder"],
                     astra_review=True).run()
    assert result.outcome == "blocked" and result.retry_later
    assert result.recovery_exhausted and len(calls) == 4
    assert result.to_json()["retry_later"] is True


def test_damaged_retry_bookkeeping_is_archived_without_new_allowance(tmp_path):
    ledger = RecoveryLedger(tmp_path, "source")
    ledger.path.parent.mkdir(parents=True)
    ledger.path.write_text('{')
    assert not ledger.failed("verify", before="same", after="same", reason="still incomplete")
    assert len(list(ledger.path.parent.glob("recovery-policy.damaged-*.json"))) == 1
    assert ledger.failed("verify", before="same", after="real progress", reason="next window")


def test_cyclic_exception_causes_cannot_break_recovery():
    from galley.recovery import transient_failure
    first, second = RuntimeError("failed"), RuntimeError("failed")
    first.__cause__, second.__cause__ = second, first
    assert not transient_failure(first)


def test_post_launch_io_failure_does_not_refund_unknown_turns(tmp_path):
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    def lost_log(spec):
        raise FileNotFoundError("completed log disappeared")
    d = _driver(book, tmp_path, spawn=lost_log, only_phases=["ladder"], max_turns=10)
    with pytest.raises(FileNotFoundError):
        d.run()
    assert d._execution_budget().remaining("ladder", 10, d.timeout_for("ladder"))[0] == 0


@pytest.mark.parametrize("kind,held", [("integrity", True), ("limit", True), ("active", False), ("incomplete", False)])
def test_code_recovery_holds_only_proven_hard_failures(tmp_path, monkeypatch, kind, held):
    from galley.engine_phases import EnginePhaseError, EnginePhases
    book = tmp_path / "Book.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    d = _driver(book, tmp_path, execution_mode="code", astra_review=True)
    def failure(self, phase):
        raise EnginePhaseError("required operation failed", kind=kind, retryable=kind == "incomplete")
    monkeypatch.setattr(EnginePhases, "run", failure)
    result = gd.DriveResult(ws)
    assert d._run_code_phase("verify", {}, result).outcome == "blocked"
    assert result.recovery_exhausted is held
