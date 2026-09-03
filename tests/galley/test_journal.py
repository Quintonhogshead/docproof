"""The decision log (galley/journal.py): every action and its recorded reason,
rendered from a run's own artifacts — deterministic, partial-run safe, and
reachable as `docproof galley journal`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docproof.__main__ import main
from galley import journal as jr

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_novel.docx"

PLAN = """# PLAN — Ford, Book 1 · general_fiction · 40,000 words

1. sweeps + normalize   $0
2. mechanical ladder    $1.40

TOTAL est. $2.80 · CAP $10

## Gate items needing a call
G1 reflection_heading sweep (25 sites) — apply?  [recommend YES]
G2 Numerals: Chicago spell-outs as tracked edits  [recommend APPLY]

G1-G2 approved by Quinton 2026-09-03 (chat): apply both.
"""

FINDINGS = {
    "source": "source/Ford - Book Original.docx",
    "cost": {"total_usd": 1.4},
    "scripted_checks": [
        {"key": "sweep_dash", "name": "Dashes", "flagged": 5, "applied": 4,
         "remaining": 1},
        {"key": "sweep_century", "name": "Centuries", "flagged": 0,
         "applied": 0, "remaining": 0},
    ],
    "normalization": {"ran": True, "quotes": 3, "spaces": 12,
                      "paragraphs": 40},
    "consistency": {"ran": True, "terms": [
        {"key": "backyard", "dominant": "backyard",
         "forms": {"back yard": 1, "backyard": 2}, "outliers": 1}]},
    "spell_scan": {"available": True, "unique": 900, "unknown": 7},
    "findings": [
        {"finding_id": "f-0001", "para_id": "body-0002",
         "error_type": "comma_splice", "original_text": "It was late, we left.",
         "corrected_text": "It was late; we left.",
         "explanation": "Two independent clauses joined by a comma.",
         "applied": True, "state": "applied", "lane": "mechanical"},
        {"finding_id": "f-0002", "para_id": "body-0001",
         "error_type": "spelling", "original_text": "recieved",
         "corrected_text": "received",
         "explanation": "i before e except after c.",
         "applied": True, "state": "applied", "lane": "mechanical"},
        {"finding_id": "f-0003", "para_id": "body-0004",
         "error_type": "number_style", "original_text": "1892",
         "corrected_text": "1892",
         "explanation": "Is this the year or the house number?",
         "queried": True, "state": "query", "lane": "mechanical"},
        {"finding_id": "f-0004", "para_id": "body-0002",
         "error_type": "comma_splice", "original_text": "It was late, we left.",
         "corrected_text": "It was late; we left.", "explanation": "",
         "status": "rejected_duplicate", "state": "dropped",
         "disposition_reason": "the same edit was already applied as f-0001",
         "lane": "mechanical"},
    ],
}

CHANGE_VERIFY = {
    "generated_at": "2026-09-03T00:00:00Z", "engine": "subagent",
    "model": "", "applied_edits": 2, "ran": True,
    "problems": [
        {"residual_id": "c-0001", "kind": "edit_damage",
         "para_id": "body-0001", "quote": "recieved",
         "problem": "The fix dropped the following word.",
         "severity": "high", "settled": "revise",
         "settlement_reason": "edit_damage:artifact"},
    ],
}
FINISHED_WALK = {
    "generated_at": "2026-09-03T00:00:00Z", "engine": "subagent",
    "paragraphs_verified": 40, "ran": True, "unread_paragraphs": [],
    "residuals": [
        {"residual_id": "r-0001", "kind": "residual", "para_id": "body-0007",
         "quote": "the the coast", "problem": "Doubled word.",
         "severity": "high", "settled": "add",
         "settlement_reason": "new edit"},
    ],
}
SETTLEMENT = {
    "run_dir": "runs/final", "rounds": 2, "engine": "subagent",
    "records": [
        {"residual_id": "r-0001", "round": 1, "action": "add",
         "owner_finding_id": None, "before_replacement": "the the coast",
         "after_replacement": "the coast", "reason": "doubled word",
         "verified_by": "subagent", "para_id": "body-0007", "kind": "residual"},
        {"residual_id": "c-0001", "round": 2, "action": "revise",
         "owner_finding_id": "f-0002", "before_replacement": "received",
         "after_replacement": "received the", "reason": "edit_damage:artifact",
         "verified_by": "subagent", "para_id": "body-0001",
         "kind": "edit_damage"},
    ],
    "open": [],
    "convergence": {"stopped": "quiet", "last_new_items": 1, "last_reread": 40,
                    "rounds": 2, "quiet": True},
}
CERTIFY = """Certificate for runs/final:
  [PASS] source hash — 2ba8bdd0cff8…
  [skip] config hash — no --config given
  [FAIL] delivered text hygiene — 1 trailing-space run(s)

  FAILED (1 failing check(s))
"""
OUTCOME = {"outcome": "done", "reason": "no open items: 2 tracked edits",
           "evidence": {"applied_edits": 2, "words": 400,
                        "edit_density_per_kword": 5.0},
           "hubspot": {"property": "docproof", "value": "Proofing Complete"},
           "set_by": "assess"}
DRIVER = {
    "workspace": "ws", "outcome": "done", "reason": "finished",
    "stopped_at": None,
    "gate": {"policy": "auto", "approved": True,
             "reason": "plan totals $2.80 within the $10.00 budget; "
                       "mechanical lines only", "total_usd": 2.8},
    "phases": [{"phase": "profile", "returncode": 0, "limit": "", "log": "x"},
               {"phase": "ladder", "returncode": 0, "limit": "", "log": "x"}],
}


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str)
                    else payload, encoding="utf-8")


@pytest.fixture()
def workspace(tmp_path) -> Path:
    """A synthetic workspace shaped like a finished mechanical run."""
    ws = tmp_path / "ford-book-1"
    run = ws / "runs" / "final"
    _write(ws / "PLAN.md", PLAN)
    _write(ws / "profile.json", {
        "genre": "general_fiction", "posture": "hammered", "word_count": 40000,
        "comment_budget": 40, "tics": [{"key": "doubled_quote"}],
        "intent_zones": [{"name": "epigraphs",
                          "reason": "wording locked, punctuation open"}],
        "bespoke_sweeps": [{"key": "reflection_heading"}]})
    _write(ws / "approval.json", {
        "source": "source/Ford - Book Original.docx", "max_spend_usd": 10.0,
        "stage": "mechanical-wave", "genre": "general_fiction",
        "enabled_lanes": ["ensemble", "repair"], "mechanical_only": True,
        "allowed_models": ["gpt-5.6-luna"], "allowed_providers": ["openai"]})
    _write(ws / "runs" / "driver" / "driver.json", DRIVER)
    _write(ws / "runs" / "certify.txt", CERTIFY)
    _write(run / "findings.json", FINDINGS)
    _write(run / "change_verify.json", CHANGE_VERIFY)
    _write(run / "finished_walk.json", FINISHED_WALK)
    _write(run / "settlement.json", SETTLEMENT)
    _write(run / "outcome.json", OUTCOME)
    return ws


@pytest.fixture()
def run_dir(workspace) -> Path:
    return workspace / "runs" / "final"


# --- the whole document ------------------------------------------------------

def test_every_phase_gets_a_section(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    for phase in ("profile", "approve", "sweeps", "ladder", "audit", "verify",
                  "settle", "certify", "deliver"):
        assert f"## {jr.PHASE_TITLES[phase]}" in text, phase
    # A mechanical run carries no empty copy-edit sections.
    assert jr.PHASE_TITLES["flights"] not in text
    assert jr.PHASE_TITLES["reread"] not in text


def test_the_render_is_deterministic(workspace, run_dir):
    first = jr.render_journal(run_dir, workspace=workspace)
    second = jr.render_journal(run_dir, workspace=workspace)
    assert first == second
    # No clock is read: a timestamp only appears when the caller supplies one.
    assert "generated" not in first.splitlines()[4]
    stamped = jr.render_journal(run_dir, workspace=workspace,
                                generated_at="2026-09-03T00:00:00Z")
    assert "generated 2026-09-03T00:00:00Z" in stamped


def test_driver_events_carry_the_gate_decision(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "**profile** — ran and finished." in text
    assert "**Plan gate (auto): approved**" in text
    assert "within the $10.00 budget" in text


def test_a_stopped_run_says_where_and_why(workspace, run_dir):
    stopped = dict(DRIVER, stopped_at="settle",
                   reason="still finding errors after 3 round(s): 9 in the "
                          "last round",
                   phases=DRIVER["phases"] + [
                       {"phase": "settle", "returncode": 0, "limit": "",
                        "log": "x"}])
    _write(workspace / "runs" / "driver" / "driver.json", stopped)
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "**The run stopped at `settle`:**" in text
    assert "still finding errors after 3 round(s)" in text


def test_a_capped_phase_is_named_as_such(workspace, run_dir):
    capped = dict(DRIVER, phases=[
        {"phase": "settle", "returncode": 124, "limit": "timeout", "log": "x"}])
    _write(workspace / "runs" / "driver" / "driver.json", capped)
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "STOPPED at its timeout cap" in text


# --- the phases --------------------------------------------------------------

def test_the_gate_items_and_their_resolution(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "| G1 | reflection_heading sweep (25 sites) — apply?" in text
    assert "| G2 | Numerals" in text
    # The range line is the RESOLUTION, not a tenth gate item.
    assert "### How the gate was resolved" in text
    assert "G1-G2 approved by Quinton" in text
    assert "| G1-G2 |" not in text
    assert "API ceiling: **$10.00**" in text
    assert "mechanical proofreading only" in text


def test_sweeps_report_counts_and_the_quiet_ones(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "| `sweep_dash` (Dashes) | 5 | 4 | 1 |" in text
    assert "Ran and found nothing: `sweep_century`." in text
    assert "12 space run(s) collapsed" in text
    assert "| `backyard` | backyard | back yard ×1, backyard ×2 | 1 |" in text
    assert "7 unknown token(s)" in text


def test_every_applied_edit_carries_its_recorded_reason(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "### Applied as tracked edits (2)" in text
    assert ('`f-0001` (comma_splice): "It was late, we left." → '
            '"It was late; we left." — Two independent clauses joined by a '
            'comma.') in text
    # Grouped by paragraph, in paragraph order.
    assert text.index("**body-0001**") < text.index("**body-0002**")


def test_queries_and_withheld_rows_say_why(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "### Author queries (1)" in text
    assert "Is this the year or the house number?" in text
    assert "### Raised and NOT applied (1)" in text
    assert "**dropped** — 1 row(s)" in text
    assert "the same edit was already applied as f-0001" in text


def test_verify_verdicts_and_settle_records(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "### Change verifier — 1 edit(s) flagged" in text
    assert "The fix dropped the following word." in text
    assert "settled: **revise**" in text
    assert "### Finished-text walk — 1 residual(s)" in text
    assert "### Round 1 — 1 item(s) (add=1)" in text
    assert "### Round 2 — 1 item(s) (revise=1)" in text
    assert "`c-0001` → **revise** (owner `f-0002`) — edit_damage:artifact" in text
    assert "stopped because: **quiet**" in text


def test_certify_checks_and_the_outcome(workspace, run_dir):
    text = jr.render_journal(run_dir, workspace=workspace)
    assert "| source hash | **PASS** |" in text
    assert "| delivered text hygiene | **FAIL** |" in text
    assert "**1 check(s) failed**" in text
    assert "**done** — no open items: 2 tracked edits" in text
    assert "| edit_density_per_kword | 5 |" in text


# --- partial runs ------------------------------------------------------------

def test_a_partial_run_renders_not_run_sections(tmp_path):
    """A run that only got as far as the ladder still renders every section —
    the ones with no artifact say so in one line."""
    ws = tmp_path / "half"
    run = ws / "runs" / "final"
    _write(run / "findings.json", FINDINGS)
    text = jr.render_journal(run, workspace=ws)
    assert "*Not run — no `profile.json` in the workspace.*" in text
    assert "no `PLAN.md` and no `approval.json`" in text
    assert "no `change_verify.json` or `finished_walk.json`" in text
    assert "no `settlement.json`" in text
    assert "no `runs/certify.txt`" in text
    assert "no `outcome.json`" in text
    # …and what DID run is still fully rendered.
    assert "### Applied as tracked edits (2)" in text


def test_an_empty_run_still_renders(tmp_path):
    run = tmp_path / "nothing"
    run.mkdir()
    text = jr.render_journal(run)
    assert text.startswith("# Decision log — nothing")
    assert "| Findings recorded | 0 |" in text
    assert text.count("*Not run") >= 8


def test_a_copyedit_run_gets_its_section(tmp_path):
    ws = tmp_path / "ce"
    run = ws / "runs" / "final"
    rows = list(FINDINGS["findings"]) + [
        {"finding_id": "f-9001", "para_id": "body-0003", "lane": "copyedit",
         "error_type": "copyedit", "original_text": "very big",
         "corrected_text": "enormous", "explanation": "Tighter.",
         "applied": True}]
    _write(run / "findings.json", dict(FINDINGS, findings=rows))
    text = jr.render_journal(run, workspace=ws)
    assert jr.PHASE_TITLES["flights"] in text
    assert "`f-9001` (copyedit)" in text
    # A copy-edit row is not counted into the mechanical ladder.
    assert "### Applied as tracked edits (2)" in text


# --- the CLI verb ------------------------------------------------------------

def test_journal_verb_writes_the_file(workspace, run_dir, tmp_path, capsys):
    out = tmp_path / "DECISION_LOG.md"
    rc = main(["galley", "journal", str(run_dir), "--workspace",
               str(workspace), "--at", "2026-09-03T00:00:00Z",
               "--out", str(out)])
    assert rc == 0
    assert "wrote" in capsys.readouterr().out
    assert out.read_text("utf-8").startswith("# Decision log —")


def test_journal_verb_prints_to_stdout_and_refuses_a_missing_run(
        workspace, run_dir, tmp_path, capsys):
    rc = main(["galley", "journal", str(run_dir), "--workspace",
               str(workspace)])
    assert rc == 0
    assert "# Decision log —" in capsys.readouterr().out
    rc = main(["galley", "journal", str(tmp_path / "nope")])
    assert rc == 2
    assert "no run directory" in capsys.readouterr().err
