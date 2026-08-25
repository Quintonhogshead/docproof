"""CLI-level coverage for `docproof import-findings` and `docproof replay`:
end-to-end, no API, against the shared `simple.docx` fixture.
"""
from __future__ import annotations

import json

from docproof.__main__ import main
from .conftest import FIXTURES

BODY_0 = "The manuscript was finished, nobody wanted to read it."
BODY_1 = "Although it was late, we kept driving toward the coast."
BODY_2 = "She opened the letter, her hands would not stay still."


def _write(path, rows):
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_import_findings_dry_run_reports_without_writing(tmp_path, capsys):
    findings = tmp_path / "findings.json"
    _write(findings, [
        {"para_id": "body-0000", "original_text": BODY_0,
         "corrected_text": BODY_0.replace(",", ";", 1)},
    ])
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out), "--dry-run", "--json"])
    assert rc == 0
    # setup_logging() creates `out` for run.log even on a dry run; the
    # deliverable itself must not exist.
    assert not (out / "simple - Atmosphere Press Proofreader.docx").exists()
    assert not (out / "findings.json").exists()
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    env = json.loads(printed)
    assert {"findings", "cost", "ledger", "checkpoint"} <= set(env)
    assert env["cost"] == {"total_usd": 0.0, "by_model": {}}
    assert env["checkpoint"] is None
    assert env["findings"][0]["status"] == "validated"


def test_import_findings_writes_a_tracked_change_with_default_type(tmp_path):
    findings = tmp_path / "findings.json"
    _write(findings, [
        {"para_id": "body-0000", "original_text": BODY_0,
         "corrected_text": BODY_0.replace(",", ";", 1)},
    ])
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out)])
    assert rc == 0
    assert (out / "simple - Atmosphere Press Proofreader.docx").exists()
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    rows = payload["findings"]
    assert len(rows) == 1
    assert rows[0]["error_type"] == "imported_edit"     # no error_type on the row
    assert rows[0]["status"] == "validated"


def test_import_findings_remaps_general_error_onto_the_edit_channel(tmp_path):
    """The Redding-stand-in trap: general_error is channel:query in the
    shipped registry. A naive replay of a row labelled that way would land as
    a margin comment, never a tracked change. import-findings must not."""
    findings = tmp_path / "findings.json"
    _write(findings, [
        {"para_id": "body-0001", "original_text": BODY_1,
         "corrected_text": BODY_1.replace("coast", "shore"),
         "error_type": "general_error"},
    ])
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out), "--json"])
    assert rc == 0
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    row = payload["findings"][0]
    assert row["error_type"] == "imported_edit"          # NOT general_error
    assert row["status"] == "validated"                  # a real tracked change
    assert row["applied"] is True


def test_import_findings_rejects_malformed_rows_with_a_reason(tmp_path, capsys):
    findings = tmp_path / "findings.json"
    _write(findings, [
        {"para_id": "body-0000", "original_text": BODY_0,
         "corrected_text": BODY_0.replace(",", ";", 1)},
        {"para_id": "", "original_text": "x", "corrected_text": "y"},
        "not a row at all",
    ])
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out), "--dry-run", "--json"])
    assert rc == 0
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    env = json.loads(printed)
    assert env["malformed"] and len(env["malformed"]) == 2
    reasons = {r["reason"] for r in env["malformed"]}
    assert any("para_id" in r for r in reasons)
    assert any("not a JSON object" in r for r in reasons)


def test_import_findings_accepts_the_contract_envelope_as_input(tmp_path):
    """A findings file can be the envelope build_envelope() itself produces —
    `docproof review --json` output, say — not just a bare array."""
    findings = tmp_path / "findings.json"
    envelope = {
        "findings": [
            {"para_id": "body-0000", "original_text": BODY_0,
             "corrected_text": BODY_0.replace(",", ";", 1)},
        ],
        "cost": {"total_usd": 0.0, "by_model": {}},
        "ledger": {"total": 0, "gaps": [], "unruled": [], "degraded": []},
        "checkpoint": None,
    }
    findings.write_text(json.dumps(envelope), encoding="utf-8")
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out)])
    assert rc == 0
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    assert len(payload["findings"]) == 1


def test_replay_preserves_a_real_query_channel_type(tmp_path):
    """replay's rows came FROM docproof — speaker_change is channel:query and
    enabled by config/default.yaml's default error_types. Unlike
    import-findings, replay must NOT force it onto the edit channel."""
    findings = tmp_path / "checkpoint.json"
    _write(findings, [
        {"para_id": "body-0002", "original_text": BODY_2,
         "corrected_text": BODY_2, "error_type": "speaker_change",
         "explanation": "who is speaking here?"},
    ])
    out = tmp_path / "out"
    rc = main(["replay", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out), "--json"])
    assert rc == 0
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    row = payload["findings"][0]
    assert row["error_type"] == "speaker_change"          # not remapped
    assert row["status"] == "query"                       # channel preserved


def test_replay_round_trips_a_findings_json_report(tmp_path):
    """The common case: replay an earlier run's own findings.json (an
    "archived job" artifact) at $0."""
    src_findings = tmp_path / "src_findings.json"
    _write(src_findings, [
        {"para_id": "body-0000", "original_text": BODY_0,
         "corrected_text": BODY_0.replace(",", ";", 1),
         "error_type": "comma_splice"},
    ])
    first_out = tmp_path / "first"
    rc = main(["review", str(FIXTURES / "simple.docx"),
              "--mock-findings", str(src_findings), "--out", str(first_out)])
    assert rc == 0
    archived = first_out / "findings.json"
    assert archived.exists()

    replay_out = tmp_path / "replayed"
    rc = main(["replay", str(archived), str(FIXTURES / "simple.docx"),
              "--out", str(replay_out), "--json"])
    assert rc == 0
    payload = json.loads((replay_out / "findings.json").read_text("utf-8"))
    matches = [r for r in payload["findings"] if r["para_id"] == "body-0000"
              and r["status"] == "validated"]
    assert matches
    assert matches[0]["error_type"] == "comma_splice"


def test_replay_sanitizes_straight_quotes_in_corrected_text(tmp_path):
    findings = tmp_path / "checkpoint.json"
    _write(findings, [
        {"para_id": "body-0000", "original_text": BODY_0,
         "corrected_text": BODY_0.replace(
             "finished", 'finished, "so it goes"')},
    ])
    out = tmp_path / "out"
    rc = main(["replay", str(findings), str(FIXTURES / "simple.docx"),
              "--out", str(out)])
    assert rc == 0
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    row = payload["findings"][0]
    assert '"' not in row["corrected_text"]
    assert "“" in row["corrected_text"]              # curled, not straight
