"""A clean final snapshot must not erase evidence of earlier repair work."""
from __future__ import annotations

import json
from pathlib import Path

from galley.journal import render_journal
from galley.run_history import load_history


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _findings(*, stamp="2026-09-06T22:00:00Z", source="source/book.docx"):
    return {"source": source, "generated_at": stamp,
            "findings": [{"finding_id": "f-1", "applied": True}]}


def _stream(path: Path, *, event="event-1", minutes=5, turns=20) -> None:
    _write(path, {"type": "result", "uuid": event,
                  "duration_ms": minutes * 60000, "num_turns": turns,
                  "subtype": "success"})


def test_final_zero_rounds_does_not_hide_prior_settlement(tmp_path):
    run = tmp_path / "runs" / "final3"
    current = _findings(stamp="2026-09-06T23:00:00Z")
    _write(run / "findings.json", current)
    _write(run / "settlement.json", {"rounds": 0, "records": []})
    _write(tmp_path / "runs/ladder/findings.json", _findings())
    _write(tmp_path / "runs/ladder/settlement.json",
           {"rounds": 2, "records": [{"action": "add"}]})
    text = render_journal(run, workspace=tmp_path)
    assert "Settle rounds recorded for this build | 0" in text
    assert "`runs/ladder` | 2026-09-06T22:00:00Z | 1 | 0 | 2 | 1" in text
    assert "`runs/final3` **(selected build)**" in text
    assert "counts are not added together" in text
    assert "This build's settlement receipt records 0 decision record(s)" in text


def test_snapshots_group_identical_copies_and_exclude_other_sources(tmp_path):
    run = tmp_path / "runs/final"
    env = _findings()
    _write(run / "findings.json", env)
    _write(tmp_path / "runs/backup/findings.json", env)
    _write(tmp_path / "runs/unrelated/findings.json",
           _findings(source="source/other-book.docx"))
    history = load_history(tmp_path, run, env)
    assert len(history.snapshots) == 1
    assert history.snapshots[0].paths == ["runs/backup", "runs/final"]
    assert history.snapshots[0].selected


def test_same_path_different_known_source_revision_is_excluded(tmp_path):
    run = tmp_path / "runs/final"
    env = dict(_findings(), source_sha256="new-source")
    _write(run / "findings.json", env)
    _write(tmp_path / "runs/old/findings.json",
           dict(_findings(), source_sha256="old-source"))
    assert len(load_history(tmp_path, run, env).snapshots) == 1


def test_receipts_recover_phases_omitted_by_resumed_driver_ledger(tmp_path):
    run = tmp_path / "runs/final"
    _write(run / "findings.json", _findings())
    _write(tmp_path / "runs/driver/driver.json", {"phases": [
        {"phase": "settle", "returncode": 0}]})
    _stream(tmp_path / "runs/driver/profile.stream.jsonl", minutes=8, turns=73)
    _stream(tmp_path / "runs/driver/settle.stream.jsonl", event="event-2",
            minutes=28, turns=108)
    text = render_journal(run, workspace=tmp_path)
    assert "profile · `runs/driver/profile.stream.jsonl` | 8.00 | 73" in text
    assert "**36.00 minutes**" in text
    assert "parent-agent turns: **181**" in text
    assert "not total elapsed runtime or time wasted" in text


def test_attempt_clock_precedes_stream_and_its_copy_without_double_count(tmp_path):
    run = tmp_path / "runs/final"
    driver = tmp_path / "runs/driver"
    _write(driver / "settle.attempt.json", {
        "attempt_id": "attempt-1", "phase": "settle", "status": "completed",
        "elapsed_s": 120, "num_turns": 7, "returncode": 0,
        "stream": "settle.stream.jsonl", "started_at": "2026-09-06T22:00:00Z"})
    _stream(driver / "settle.stream.jsonl", minutes=1, turns=7)
    _stream(driver / "attempts/copy/settle.stream.jsonl", minutes=1, turns=7)
    _stream(driver / "settle.recovery.stream.jsonl", event="event-2",
            minutes=3, turns=9)
    history = load_history(tmp_path, run, None)
    assert len(history.sessions) == 2
    assert sum(s.duration_ms for s in history.sessions) == 300000
    assert sum(s.num_turns for s in history.sessions) == 16
    assert {s.timing_source for s in history.sessions} == {"driver clock", "CLI result"}


def test_copied_attempt_receipts_and_legacy_results_each_count_once(tmp_path):
    driver = tmp_path / "runs/driver"
    attempt = {"attempt_id": "one", "phase": "audit", "elapsed_s": 60,
               "num_turns": 4, "status": "completed"}
    _write(driver / "audit.attempt.json", attempt)
    _write(driver / "attempts/copy/audit.attempt.json", attempt)
    _stream(driver / "profile.stream.jsonl")
    _stream(driver / "attempts/copy/profile.stream.jsonl")
    history = load_history(tmp_path, tmp_path / "runs/final", None)
    assert len(history.sessions) == 2
    assert all(len(s.paths) == 2 for s in history.sessions)


def test_incomplete_and_malformed_evidence_does_not_become_zero_work(tmp_path):
    run = tmp_path / "runs/final"
    _write(run / "findings.json", _findings())
    driver = tmp_path / "runs/driver"
    _write(driver / "verify.attempt.json", {"attempt_id": "running",
        "phase": "verify", "status": "running", "stream": "verify.stream.jsonl"})
    _write(driver / "verify.stream.jsonl", {"type": "assistant"})
    _write(driver / "audit.stream.jsonl", {"type": "assistant"})
    _write(driver / "profile.stream.jsonl", {"type": "result",
        "uuid": "unknown-metrics", "duration_ms": None, "num_turns": False})
    (driver / "bad.attempt.json").write_text("truncated{", encoding="utf-8")
    text = render_journal(run, workspace=tmp_path)
    assert "not recorded | not recorded | running | driver clock" in text
    assert "Session streams with no completion receipt" in text
    assert "`runs/driver/audit.stream.jsonl`" in text
    assert "durations total" not in text


def test_adjudication_section_uses_report_not_inferred_phase_state(tmp_path):
    from galley.journal import JournalSources, _Doc, _section_adjudicate

    src = JournalSources(run_dir=tmp_path / "runs/final", workspace=tmp_path)
    doc = _Doc()
    _section_adjudicate(doc, src)
    assert "later phase state alone does not establish" in doc.render()
    report = tmp_path / "runs/ADJUDICATE.md"
    report.parent.mkdir()
    report.write_text("Included 27 sweep rows; retained corrected build runs/final.")
    doc = _Doc()
    _section_adjudicate(doc, src)
    assert "> Included 27 sweep rows" in doc.render()


def test_installed_execution_ledger_does_not_report_reservations_as_work(tmp_path):
    driver = tmp_path / "runs/driver"
    _write(driver / "execution-budget.json", {"attempts": [
        {"id": "running", "phase": "profile", "status": "running",
         "turns": 80, "seconds": 7200, "reserved_turns": 80,
         "reserved_seconds": 7200, "log": str(driver / "profile-uuid.log")},
        {"id": "unknown-turns", "phase": "audit", "status": "completed",
         "turns": 80, "seconds": 60, "usage_known": True,
         "log": str(driver / "audit-uuid.log")}]})
    history = load_history(tmp_path, tmp_path / "runs/final", None)
    running = next(s for s in history.sessions if s.phase == "profile")
    audit = next(s for s in history.sessions if s.phase == "audit")
    assert running.duration_ms is None and running.num_turns is None
    assert audit.duration_ms == 60000 and audit.num_turns is None


def test_installed_execution_ledger_reuses_actual_stream_measurement_once(tmp_path):
    driver = tmp_path / "runs/driver"
    _write(driver / "execution-budget.json", {"attempts": [
        {"id": "coordinator", "phase": "profile", "status": "completed",
         "turns": 80, "seconds": 360, "usage_known": True,
         "log": str(driver / "profile-uuid.log")},
        {"id": "code", "phase": "code-verify", "status": "completed",
         "turns": 0, "seconds": 60, "usage_known": True,
         "log": str(driver / "verify-uuid.log")}]})
    _stream(driver / "profile-uuid.stream.jsonl", minutes=5, turns=7)
    _stream(driver / "backup/profile.stream.jsonl", minutes=5, turns=7)
    history = load_history(tmp_path, tmp_path / "runs/final", None)
    assert len(history.sessions) == 2
    assert sum(s.duration_ms for s in history.sessions) == 420000
    assert [s.num_turns for s in history.sessions if s.phase == "profile"] == [7]
    assert [s.num_turns for s in history.sessions if s.phase == "code-verify"] == [None]


def test_installed_resource_per_model_rows_are_one_coordinator_receipt(tmp_path):
    driver = tmp_path / "runs/driver"
    _write(driver / "execution-budget.json", {"attempts": [
        {"id": "one", "phase": "profile", "status": "completed",
         "turns": 80, "seconds": 120, "usage_known": True,
         "log": str(driver / "profile-uuid.log")}]})
    rows = [{"receipt_id": "one", "operation_id": "coordinator-profile-one",
             "status": "completed", "model": model,
             "metadata": {"num_turns": 8, "duration_ms": 110000}}
            for model in ("main", "helper")]
    (driver / "resources.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")
    history = load_history(tmp_path, tmp_path / "runs/final", None)
    assert len(history.sessions) == 1
    assert history.sessions[0].duration_ms == 120000
    assert history.sessions[0].num_turns == 8


def test_installed_journal_keeps_existing_notes_and_recovery(tmp_path):
    run = tmp_path / "runs/final"
    _write(run / "findings.json", _findings())
    (tmp_path / "QUESTIONS.md").write_text("Retained local observation.")
    _write(tmp_path / "runs/driver/driver.json", {"recovery": [
        {"phase": "audit", "reason": "Reused prior evidence", "log": "audit.log"}]})
    text = render_journal(run, workspace=tmp_path)
    assert "Local notes retained for final review" in text
    assert "Retained local observation." in text
    assert "**Automatic recovery (audit):** Reused prior evidence" in text
