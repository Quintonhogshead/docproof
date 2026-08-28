"""`docproof review --dry-run` (P2-10): the authoritative no-API price for the
plan gate. It must estimate the exact config's cost and make no provider call —
so the plan gate stops pricing the Luna ladder from a stale calibration.
"""
from __future__ import annotations

from pathlib import Path

from docproof.__main__ import main

FIXTURE = Path("tests/fixtures/simple.docx")


def test_dry_run_prices_without_spending(capsys, tmp_path, monkeypatch):
    # Any attempt to build a provider (the only thing that can spend) is a test
    # failure: a dry run must never reach the API.
    import docproof.__main__ as m

    def _boom(*a, **k):                       # pragma: no cover - must not run
        raise AssertionError("dry-run reached the provider")
    monkeypatch.setattr(m, "build_provider", _boom)

    rc = main(["review", str(FIXTURE), "--config", "config/default.yaml",
               "--dry-run", "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "no API call made" in out
    # No deliverable was written.
    assert not any(tmp_path.glob("*.docx"))
