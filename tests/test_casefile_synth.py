"""P1-6: `galley letter` on a bare `review`/`replay` run.

Only the orchestrator and the hosted-app path write casefile.json; a plain CLI
run writes findings.json and no case file, so the letter could not be produced
on it without hand-authoring the file. The synthesizer projects the case file the
letter needs from findings.json; the letter fallback wires it in.
"""
from __future__ import annotations

import json

from docproof.__main__ import main
from galley.casefile_synth import casefile_from_run


def _write_run(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({
        "source": "My Book.docx",
        "cost": {"total_usd": 0.64},
        "findings": [
            {"finding_id": "s-0001", "para_id": "body-0",
             "error_type": "spelling", "original_text": "teh",
             "corrected_text": "the", "explanation": "typo",
             "confidence": "high", "status": "validated",
             "anchor": {"start": 0, "end": 3}},
            {"finding_id": "c-0002", "para_id": "body-1",
             "error_type": "term_consistency", "original_text": "check in",
             "corrected_text": "check in", "explanation": "open vs hyphen?",
             "confidence": "medium", "status": "query", "force_query": True},
        ]}), encoding="utf-8")
    return tmp_path


def test_casefile_from_run_projects_findings_verdicts_and_spend(tmp_path):
    cf = casefile_from_run(_write_run(tmp_path))
    assert cf.book == "My Book"
    assert len(cf.findings) == 2
    assert cf.budget.spent_usd == 0.64
    rulings = {v.finding_id: v.ruling for v in cf.verdicts}
    # Case-file vocabulary (galley.contracts.RULINGS): an applied edit is "keep".
    assert rulings == {"s-0001": "keep", "c-0002": "query"}
    assert cf.waves and cf.waves[0].findings_added == 2
    # The wave's scope is the orchestrator's dict shape, so record_run prices it.
    scope = cf.waves[0].actions[0]["scope"]
    assert isinstance(scope, dict) and scope["chapters"] == []


def test_galley_letter_runs_on_a_bare_run_directory(tmp_path):
    _write_run(tmp_path)
    rc = main(["galley", "letter", str(tmp_path)])
    assert rc == 0
    letter = (tmp_path / "letter.md").read_text(encoding="utf-8")
    assert "Editorial letter — My Book" in letter
    assert (tmp_path / "style-sheet.md").exists()


def test_galley_letter_errors_without_findings_or_casefile(tmp_path, capsys):
    rc = main(["galley", "letter", str(tmp_path)])
    assert rc == 2
    assert "no findings.json" in capsys.readouterr().err
