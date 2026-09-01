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


# --- prepare() itself must not spend on a dry run ---------------------------
#
# Redding Book 1 (2026-09-01): prepare() builds the story-sheet provider and
# reads the whole book. On a mechanical-wave config (storysheet ON) that made
# `review --dry-run` and `galley flights --dry-run` crash with no API key — and,
# with one, bill a whole-book read to answer "what would this cost?".

def _storysheet_config(tmp_path) -> Path:
    """The shipped config with the two stages prepare() can spend on ON."""
    import yaml
    cfg = yaml.safe_load(Path("config/default.yaml").read_text(encoding="utf-8"))
    cfg["storysheet"]["enabled"] = True
    path = tmp_path / "storysheet.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def _no_provider(monkeypatch):
    """Any provider build anywhere is a test failure — including the one
    prepare() reaches through docproof.providers, which the CLI-level patch in
    the test above never covered."""
    import docproof.providers as providers

    def _boom(*a, **k):                       # pragma: no cover - must not run
        raise AssertionError("dry-run built a provider")
    monkeypatch.setattr(providers, "build_provider", _boom)
    import docproof.__main__ as m
    monkeypatch.setattr(m, "build_provider", _boom)


def test_review_dry_run_skips_the_story_sheet(capsys, tmp_path, monkeypatch):
    _no_provider(monkeypatch)
    rc = main(["review", str(FIXTURE), "--config",
               str(_storysheet_config(tmp_path)), "--dry-run",
               "--out", str(tmp_path)])
    assert rc == 0
    assert "no API call made" in capsys.readouterr().out


def test_galley_flights_dry_run_skips_the_story_sheet(capsys, tmp_path,
                                                      monkeypatch):
    _no_provider(monkeypatch)
    rc = main(["galley", "flights", str(FIXTURE), "--config",
               str(_storysheet_config(tmp_path)), "--dry-run",
               "--out", str(tmp_path)])
    assert rc == 0
    assert "Dry run" in capsys.readouterr().out


def test_prepare_dry_run_leaves_the_story_sheet_empty(tmp_path, monkeypatch):
    """The kwarg itself, without the CLI: no provider, and an empty sheet."""
    import docproof.providers as providers
    from docproof.config import load_config
    from docproof.pipeline import prepare

    def _boom(*a, **k):                       # pragma: no cover - must not run
        raise AssertionError("prepare(dry_run=True) built a provider")
    monkeypatch.setattr(providers, "build_provider", _boom)

    cfg = load_config(str(_storysheet_config(tmp_path)))
    cfg.output_dir = str(tmp_path)
    prepared = prepare(cfg, str(FIXTURE), "config/error_types", dry_run=True)
    assert prepared.story_sheet == ""
    assert prepared.candidate_screening is None
