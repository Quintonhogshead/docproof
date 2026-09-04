"""`docproof galley drive` through the real argument parser — the verb wiring,
its exit codes, and `--print-prompt` (what the thin shell wrapper calls). No
session is ever spawned: the spawner is patched at the module seam.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docproof.__main__ import main
from galley import driver as gd

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_novel.docx"

PLAN = ("# PLAN — Ford\n\n1. sweeps $0\n2. ladder $1.40\n\n"
        "TOTAL est. $2.80 · CAP $10\n")


@pytest.fixture()
def book(tmp_path) -> Path:
    dest = tmp_path / "Ford - Book 1.docx"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def test_print_prompt_is_what_the_shell_wrapper_reads(book, capsys):
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--print-prompt", "ladder"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Phase: mechanical ladder" in out
    assert "MECHANICAL PROOFREADING ONLY" not in out    # no note on this phase


def test_print_prompt_refuses_a_copyedit_phase_in_scope(book, capsys):
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--print-prompt", "flights"])
    assert rc == 2
    assert "copy-edit scope" in capsys.readouterr().err
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--copyedit", "--print-prompt", "flights"])
    assert rc == 0
    assert "flight-deck" in capsys.readouterr().out


def test_dry_run_seeds_the_workspace_and_prints_the_sequence(book, tmp_path,
                                                             capsys):
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--workspace-root", str(tmp_path / "ws"), "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["phases"] == list(gd.MECHANICAL_PHASES)
    assert payload["mechanical_only"] is True
    assert payload["budget_usd"] == gd.DEFAULT_BUDGET_USD
    assert (Path(payload["workspace"]) / "CLAUDE.md").is_file()


def test_drive_stops_at_the_gate_and_exits_7(book, tmp_path, capsys,
                                             monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    calls: list[str] = []

    def fake(spec: gd.PhaseSpec) -> gd.PhaseResult:
        calls.append(spec.phase)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text("profiled\n", encoding="utf-8")
        (spec.workspace / "PLAN.md").write_text(PLAN, encoding="utf-8")
        return gd.PhaseResult(spec.phase, 0, spec.log_path, "profiled")

    monkeypatch.setattr(gd, "spawn_claude", fake)
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--workspace-root", str(tmp_path / "ws"), "--budget", "1",
               "--no-state-gate", "--json"])
    # profile ran, the gate refused the $2.80 plan against a $1 budget.
    assert calls == ["profile"]
    assert rc == 7
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["outcome"] == "needs_human"
    assert "over the $1.00 budget" in payload["reason"]
    ws = tmp_path / "ws" / "ford"
    assert json.loads((ws / "runs" / "outcome.json").read_text(
        "utf-8"))["outcome"] == "needs_human"


def test_drive_reports_a_setup_error_as_exit_2(book, tmp_path, capsys):
    rc = main(["galley", "drive", "--book", str(book), "--slug", "ford",
               "--workspace-root", str(tmp_path / "ws"),
               "--phases", "flights"])
    # An out-of-scope phase is the caller's mistake: exit 2, and NO
    # needs_human outcome.json for DocWatch to act on.
    assert rc == 2
    assert "copy-edit scope" in capsys.readouterr().err
    assert not (tmp_path / "ws" / "ford" / "runs" / "outcome.json").exists()
    rc = main(["galley", "drive", "--book", str(tmp_path / "missing.docx"),
               "--slug", "ford", "--workspace-root", str(tmp_path / "ws"),
               "--dry-run"])
    assert rc == 2
    assert "no manuscript" in capsys.readouterr().err


def test_approve_records_the_mechanical_only_scope(tmp_path, capsys):
    src = tmp_path / "book.docx"
    src.write_bytes(b"fake manuscript")
    out = tmp_path / "approval.json"
    rc = main(["galley", "approve", str(src), "--config",
               "config/default.yaml", "--stage", "mechanical-wave",
               "--budget", "10", "--out", str(out), "--mechanical-only"])
    assert rc == 0
    assert json.loads(out.read_text("utf-8"))["mechanical_only"] is True
    assert "MECHANICAL ONLY" in capsys.readouterr().out


def test_approve_refuses_mechanical_only_over_a_copyedit_config(tmp_path,
                                                               capsys):
    src = tmp_path / "book.docx"
    src.write_bytes(b"fake manuscript")
    rc = main(["galley", "approve", str(src), "--config",
               "config/default.yaml", "--stage", "copyedit-wave",
               "--budget", "10", "--out", str(tmp_path / "a.json"),
               "--mechanical-only"])
    assert rc == 2
    assert "mechanical-only approval" in capsys.readouterr().err
