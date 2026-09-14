"""The per-stage timeline is read from the durable evidence a run keeps."""
import json
from pathlib import Path

import pytest

from galley.fixed_timeline import attempts, fixed_directory, render, summarize, write_timeline


def _call(directory, sha, *, stage, model, effort, status, attempt, usage, events, api_usd=None):
    call = directory / "calls" / "calls" / sha
    (call / "attempts" / str(attempt)).mkdir(parents=True)
    (call / "receipt.json").write_text(json.dumps({
        "request_sha256": sha, "stage": stage, "model": model, "effort": effort,
        "status": status, "attempt": attempt, "max_attempts": 3,
        "transport": "api" if model.startswith("gpt") else "claude_subscription",
        "failure_category": None if status == "completed" else "invalid_coverage",
        "usage": usage}))
    (call / "attempts" / str(attempt) / "transport-usage.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    return {"request_sha256": sha, "attempt": attempt, "status": status, "usage": usage,
            "api_usd": api_usd, "reserved_output_tokens": 1000, "reserved_api_usd": 0.1}


@pytest.fixture
def run(tmp_path):
    fixed = tmp_path / "work" / "runs" / "fixed"
    (fixed / "calls").mkdir(parents=True)
    entries = {}
    usage = {"input_tokens": 100, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50,
             "output_tokens": 40, "thinking_tokens": 10}
    entries["a" * 64 + ":1"] = _call(fixed, "a" * 64, stage="typed", model="claude-sonnet-5", effort="low",
        status="completed", attempt=1, usage=usage,
        events=[{"status": "started", "recorded_at": "2026-09-14T02:50:00+00:00"},
                {"status": "completed", "recorded_at": "2026-09-14T02:50:20+00:00"}])
    entries["b" * 64 + ":1"] = _call(fixed, "b" * 64, stage="typed", model="claude-sonnet-5", effort="low",
        status="completed", attempt=1, usage=usage,
        events=[{"status": "started", "recorded_at": "2026-09-14T02:50:05+00:00"},
                {"status": "completed", "recorded_at": "2026-09-14T02:51:05+00:00"}])
    # A failed first attempt and a completed second one on a Luna screen.
    failed = _call(fixed, "c" * 64, stage="typed_screen", model="gpt-5.6-luna", effort="low",
        status="completed", attempt=2, usage=usage,
        events=[{"status": "started", "recorded_at": "2026-09-14T03:00:00+00:00"},
                {"status": "completed", "recorded_at": "2026-09-14T03:00:30+00:00"}], api_usd=0.02)
    (fixed / "calls" / "calls" / ("c" * 64) / "attempts" / "1").mkdir()
    (fixed / "calls" / "calls" / ("c" * 64) / "attempts" / "1" / "transport-usage.jsonl").write_text(
        json.dumps({"status": "started", "recorded_at": "2026-09-14T02:59:00+00:00"}) + "\n"
        + json.dumps({"status": "failed", "recorded_at": "2026-09-14T02:59:40+00:00"}) + "\n")
    entries["c" * 64 + ":1"] = {**failed, "attempt": 1, "status": "failed", "api_usd": 0.01,
                                "usage": {**usage, "output_tokens": 5}}
    entries["c" * 64 + ":2"] = failed
    (fixed / "calls" / "budget.json").write_text(json.dumps({"limits": {}, "entries": entries}))
    return tmp_path / "work"


def test_workspace_or_fixed_directory_is_accepted(run):
    assert fixed_directory(run) == run / "runs" / "fixed"
    assert fixed_directory(run / "runs" / "fixed") == run / "runs" / "fixed"
    with pytest.raises(FileNotFoundError):
        fixed_directory(run / "nothing")


def test_every_attempt_is_a_row_with_its_own_usage_and_duration(run):
    rows = {(r["request_sha256"][0], r["attempt"]): r for r in attempts(run)}
    assert len(rows) == 4
    assert rows[("a", 1)]["seconds"] == 20 and rows[("b", 1)]["seconds"] == 60
    assert rows[("c", 1)]["status"] == "failed" and rows[("c", 1)]["final"] is False
    assert rows[("c", 1)]["usage"]["output_tokens"] == 5 and rows[("c", 1)]["api_usd"] == 0.01
    assert rows[("c", 2)]["status"] == "completed" and rows[("c", 2)]["seconds"] == 30


def test_stages_are_ordered_by_first_start_with_spans_medians_and_failures(run):
    report = summarize(run)
    assert [(s["stage"], s["model"]) for s in report["stages"]] == [
        ("typed", "claude-sonnet-5"), ("typed_screen", "gpt-5.6-luna")]
    typed, screen = report["stages"]
    assert typed["attempts"] == 2 and typed["failed_attempts"] == 0
    assert typed["span_seconds"] == 65 and typed["median_seconds"] == 60 and typed["max_seconds"] == 60
    assert typed["tokens"]["cache_read_input_tokens"] == 1800
    assert screen["attempts"] == 2 and screen["failed_attempts"] == 1
    assert screen["api_usd"] == 0.03 and screen["span_seconds"] == 90
    assert report["run"]["attempts"] == 4 and report["run"]["failed_attempts"] == 1
    assert report["run"]["span_seconds"] == 630 and report["run"]["api_usd"] == 0.03
    assert report["run"]["tokens"]["output_tokens"] == 40 * 3 + 5


def test_render_and_write_are_diagnostics_beside_the_result(run):
    text = render(summarize(run))
    assert "typed_screen" in text and "failed 1" in text and "span 10.5 min" in text
    target = write_timeline(run)
    assert target == run / "runs" / "fixed" / "timeline.json"
    assert json.loads(target.read_text())["run"]["attempts"] == 4
    assert not list((run / "runs" / "fixed" / "calls").glob("timeline*"))


def test_missing_ledgers_leave_blanks_not_numbers(tmp_path):
    fixed = tmp_path / "runs" / "fixed"
    (fixed / "calls" / "calls" / ("d" * 64) / "attempts" / "1").mkdir(parents=True)
    (fixed / "calls" / "calls" / ("d" * 64) / "receipt.json").write_text(json.dumps(
        {"request_sha256": "d" * 64, "stage": "fable", "model": "claude-fable-5-1", "effort": "high",
         "status": "completed", "attempt": 1, "usage": {"output_tokens": 7}}))
    report = summarize(tmp_path)
    assert report["run"]["span_seconds"] is None
    assert report["stages"][0]["median_seconds"] is None
    assert report["stages"][0]["tokens"]["output_tokens"] == 7
    assert "no transport timestamps" in render(report)


def test_cli_prints_the_table_and_the_json(run, capsys):
    from docproof.__main__ import main
    assert main(["galley", "fixed-timeline", str(run)]) in (0, None)
    assert "typed_screen" in capsys.readouterr().out
    assert main(["galley", "fixed-timeline", str(run), "--json", "--write"]) in (0, None)
    assert json.loads(capsys.readouterr().out)["run"]["attempts"] == 4
    assert (run / "runs" / "fixed" / "timeline.json").exists()
    assert main(["galley", "fixed-timeline", str(run / "nothing")]) == 2
