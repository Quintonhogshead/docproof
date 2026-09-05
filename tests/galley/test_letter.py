"""Golden-ish tests for the editorial-letter and style-sheet renderers.

Rather than commit a byte-exact golden file (brittle against wording tweaks),
each test asserts on the *structure and load-bearing substrings* that the spec
guarantees: every wave index, every open query, the total spend, every ruling
subject. That keeps the tests honest to the contract without pinning prose.
"""

from __future__ import annotations

from galley.casefile import BudgetLedger, CaseFile, Charge, Ruling
from galley.contracts import Verdict, WaveRecord
from galley.letter import render_all, render_letter, render_style_sheet

from tests.galley.fakes import gfinding, make_manuscript


def _scripted_casefile() -> CaseFile:
    """A small but complete case file: two waves, findings, queries, rulings."""

    ms = make_manuscript(
        "The qwick brown fox.",
        "It jumpd over the dog.",
        "A calm secend chapter.",
        "Nothing to see here.",
        chapter_size=2,
    )

    findings = [
        gfinding("F1", "body-0001", "qwick", "quick", wave=1),
        gfinding("F2", "body-0002", "jumpd", "jumped", wave=1),
        gfinding(
            "F3", "body-0003", "secend", "second",
            wave=2, note="Could be 'second' or a proper noun?",
        ),
    ]

    verdicts = [
        Verdict("F1", "keep", reason="clear typo", judge="fake-judge", wave=1),
        Verdict(
            "F3", "query",
            reason="ambiguous: 'secend' may be an intentional coinage",
            judge="fake-judge", wave=2,
        ),
    ]

    waves = [
        WaveRecord(
            index=1,
            actions=(
                {
                    "adapter": "languagetool",
                    "scope": "whole-book",
                    "cost_usd": 0.10,
                    "findings_added": 2,
                    "coverage_notes": [],
                },
            ),
            spend_usd=0.10,
            findings_added=2,
            started_at="2026-08-23T10:00:00Z",
            ended_at="2026-08-23T10:01:00Z",
        ),
        WaveRecord(
            index=2,
            actions=(
                {
                    "adapter": "llm-proofreader",
                    "scope": "chapter-2",
                    "cost_usd": 0.40,
                    "findings_added": 1,
                    "coverage_notes": ["chapter 2 dialogue not fully covered"],
                },
            ),
            spend_usd=0.40,
            findings_added=1,
            started_at="2026-08-23T10:02:00Z",
            ended_at="2026-08-23T10:05:00Z",
        ),
    ]

    budget = BudgetLedger(
        charges=[
            Charge("languagetool", 0.10, wave=1),
            Charge("llm-proofreader", 0.40, wave=2),
        ]
    )

    return CaseFile(
        book="Test Book",
        style_sheet=[
            Ruling("traveled / travelled", "traveled", "US spelling", "CMOS 7.1", 1),
            Ruling("serial comma", "use it", "house style", "house: comma policy", 1),
        ],
        findings=findings,
        verdicts=verdicts,
        waves=waves,
        budget=budget,
    ), ms


def test_render_letter_covers_waves_queries_and_spend(tmp_path):
    cf, ms = _scripted_casefile()
    path = render_letter(cf, tmp_path, ms=ms, recall={"overall": 0.62})

    assert path == tmp_path / "letter.md"
    text = path.read_text(encoding="utf-8")

    # Every wave index appears.
    for wave in cf.waves:
        assert f"Wave {wave.index}" in text

    # Every query verdict's finding_id AND reason appear — nothing hidden.
    query_verdicts = [v for v in cf.verdicts if v.ruling == "query"]
    assert query_verdicts, "test fixture must contain at least one query"
    for v in query_verdicts:
        assert v.finding_id in text
        assert v.reason in text

    # Total spend appears.
    assert "$0.50" in text

    # Adapters/actions summarized.
    assert "languagetool" in text
    assert "llm-proofreader" in text

    # Coverage hole surfaced.
    assert "chapter 2 dialogue not fully covered" in text

    # Confidence statement with honest caveat.
    assert "62.0%" in text
    assert "blind spot" in text.lower()

    # Per-chapter counts when ms is provided.
    assert "Chapter" in text


def test_letter_reports_clean_coverage_and_no_queries():
    """A run with no coverage notes and no query verdicts says so explicitly."""

    cf = CaseFile(
        book="Clean Book",
        waves=[
            WaveRecord(
                index=1,
                actions=({"adapter": "languagetool", "coverage_notes": []},),
                spend_usd=0.0,
                findings_added=0,
            )
        ],
    )
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        text = render_letter(cf, d).read_text(encoding="utf-8")

    assert "clean" in text.lower()
    assert "No open queries" in text


def test_query_finding_without_verdict_reason_falls_back_to_note(tmp_path):
    """A query verdict with no reason still surfaces the finding's note."""

    cf = CaseFile(
        book="Fallback",
        findings=[gfinding("F9", "body-0001", "teh", "the", note="typo note here")],
        verdicts=[Verdict("F9", "query", reason="")],
        waves=[WaveRecord(index=1)],
    )
    text = render_letter(cf, tmp_path).read_text(encoding="utf-8")
    assert "F9" in text
    assert "typo note here" in text


def test_render_style_sheet_lists_every_subject(tmp_path):
    cf, _ = _scripted_casefile()
    path = render_style_sheet(cf, tmp_path)

    assert path == tmp_path / "style-sheet.md"
    text = path.read_text(encoding="utf-8")

    for ruling in cf.style_sheet:
        assert ruling.subject in text
        assert ruling.decision in text
        assert ruling.source in text


def test_style_sheet_stub_when_empty(tmp_path):
    cf = CaseFile(book="No Rulings")
    text = render_style_sheet(cf, tmp_path).read_text(encoding="utf-8")
    assert "No rulings were recorded" in text


def test_render_all_writes_both(tmp_path):
    cf, ms = _scripted_casefile()
    letter, style = render_all(cf, tmp_path, ms=ms, recall={"overall": 0.5})

    assert letter == tmp_path / "letter.md"
    assert style == tmp_path / "style-sheet.md"
    assert letter.exists()
    assert style.exists()


def test_recall_accepts_object_and_percentage_scale(tmp_path):
    """recall may be an object, and a 0..100 rate is rendered as a percentage."""

    class Est:
        overall = 84.0

    cf = CaseFile(book="Obj", waves=[WaveRecord(index=1)])
    text = render_letter(cf, tmp_path, recall=Est()).read_text(encoding="utf-8")
    assert "84.0%" in text


def test_open_queries_omit_duplicate_refinds(tmp_path):
    """A later wave re-finding an edit wave one already made is merged, not a
    question for the author — it must not pad the open-queries list."""
    from galley.adjudicate import arbitrate

    cf, _ms = _scripted_casefile()
    cf.findings.append(gfinding("F1b", "body-0001", "qwick", "quick", wave=2))
    res = arbitrate(cf.findings)
    dup = [v for v in res.verdicts if v.finding_id == "F1b"]
    assert dup and dup[0].reason.startswith("duplicate of F1")
    cf.verdicts.extend(res.verdicts)
    text = render_letter(cf, tmp_path).read_text(encoding="utf-8")
    queries = text.split("## Open queries")[1].split("## Confidence")[0]
    assert "F3" in queries                 # the genuine query is still listed
    assert "F1b" not in queries and "duplicate of" not in queries


def test_cross_book_recall_is_not_presented_as_this_books_gauge(tmp_path):
    from galley.calibration import BookRecall

    cf, _ms = _scripted_casefile()
    cf.book = "This Book"
    # A gauge measured on another manuscript: no recall figure is claimed.
    foreign = BookRecall(planted=8, caught=6, rate=0.75, book="Other Book")
    text = render_letter(cf, tmp_path, recall=foreign).read_text(encoding="utf-8")
    assert "Against our seeded gauge" not in text
    assert "75.0%" not in text
    assert "No recall estimate was supplied" in text
    # The same book's gauge renders as before.
    own = BookRecall(planted=8, caught=6, rate=0.75, book="This Book")
    text = render_letter(cf, tmp_path, recall=own).read_text(encoding="utf-8")
    assert "Against our seeded gauge, estimated recall is **75.0%**" in text
    assert "cross-book" not in text
    # A gauge that names no book (a legacy record) is labelled, not hidden.
    untagged = {"overall": 0.5}
    text = render_letter(cf, tmp_path, recall=untagged).read_text(encoding="utf-8")
    assert "cross-book gauge" in text and "50.0%" in text


# --- the three documents, from run evidence (Georgis head-to-head) -----------

import json


def _evidence_run(tmp_path):
    ws = tmp_path / "ws"
    run = ws / "runs" / "final"
    run.mkdir(parents=True)
    rows = [
        {"finding_id": "f-1", "para_id": "body-0001", "error_type":
         "serial_comma", "status": "validated", "applied": True,
         "original_text": "a, b and c", "corrected_text": "a, b, and c"},
        {"finding_id": "f-2", "para_id": "body-0002", "error_type":
         "comma_splice", "status": "validated", "applied": True,
         "original_text": "x, y", "corrected_text": "x; y"},
        {"finding_id": "f-3", "para_id": "body-0003", "error_type":
         "imported_edit", "status": "query", "queried": True,
         "applied": False, "original_text": "Mrs. Rodewell",
         "corrected_text": "Mrs. Rodewell",
         "explanation": "Is the surname Rodewall or Rodewell? Both appear."},
        {"finding_id": "f-4", "para_id": "body-0004", "error_type":
         "imported_edit", "status": "query", "queried": True,
         "applied": False, "original_text": "twelve years",
         "corrected_text": "twelve years",
         "explanation": "Thirteen years by the dates given; which is right?"},
    ]
    (run / "findings.json").write_text(json.dumps({
        "findings": rows, "cost": {"total_usd": 0.0},
        "stats": {"validated": 2, "query": 2},
        "normalization": {"ran": True, "spaces": 12, "quotes": 1,
                          "paragraphs": 9}}), "utf-8")
    (run / "settlement.json").write_text(json.dumps({
        "rounds": 2, "counts": {"add": 3, "drop": 2, "query": 1},
        "records": [], "open": [], "residuals_seen": [1, 2, 3, 4, 5, 6],
        "convergence": {"last_new_items": 1, "quiet": True}}), "utf-8")
    (run / "change_verify.json").write_text(json.dumps({
        "ran": True, "applied_edits": 2, "problems": [{"x": 1}],
        "settled": True}), "utf-8")
    (run / "finished_walk.json").write_text(json.dumps({
        "ran": True, "paragraphs": 9, "residuals": [{"x": 1}, {"x": 2}],
        "unread_paragraphs": []}), "utf-8")
    (run / "outcome.json").write_text(json.dumps({
        "outcome": "done", "reason": "no open items"}), "utf-8")
    (ws / "profile.json").write_text(json.dumps({
        "genre": "literary_memoir", "variant": "us", "word_count": 9000,
        "comment_budget": 9,
        "proper_nouns": [{"name": "Katena", "count": 70},
                         {"name": "bronchoscopy", "count": 8},
                         {"name": "IL", "count": 2}]}), "utf-8")
    (ws / "approval.json").write_text(json.dumps({
        "source": "source/Book.docx", "mechanical_only": True,
        "comment_budget": 9}), "utf-8")
    (ws / "runs" / "certify.txt").write_text(
        "Certificate for runs/final:\n"
        "  [PASS] source hash — abc…\n"
        "  [skip] intent-zone collisions — no intent-zone record for this run\n"
        "  [PASS] comment budget — 2 comment(s), ceiling 9\n"
        "\n  PASSED (0 failing check(s))\n", "utf-8")
    return run, ws


def test_the_letter_from_evidence_reads_like_a_proofreaders_letter(tmp_path):
    from galley.casefile_synth import casefile_from_run
    from galley.letter import run_evidence

    run, ws = _evidence_run(tmp_path)
    cf = casefile_from_run(run)
    ev = run_evidence(run, ws)
    text = render_letter(cf, tmp_path / "out", evidence=ev).read_text("utf-8")

    assert "# Proofreading letter" in text
    assert "**2 tracked correction(s)**" in text
    assert "**2 margin comment(s)**" in text
    for heading in ("## Choices and reasons", "## Decisions still needed",
                    "## Verification and limits", "## Preparation disclosure"):
        assert heading in text
    # choices: the family table and the scope line
    assert "the serial comma | 1" in text and "comma splices | 1" in text
    assert "mechanical proofreading only" in text
    # decisions: one bullet per distinct question, with its site
    assert "Rodewall or Rodewell" in text and "body-0003" in text
    assert "Thirteen years" in text
    # verification: the skipped check is named, the limits stated
    assert "intent-zone collisions" in text
    assert "no defensible statistical estimate" in text
    assert "12 space run(s)" in text
    assert "**Outcome: done.**" in text


def test_the_style_sheet_from_evidence_is_never_empty(tmp_path):
    from galley.casefile_synth import casefile_from_run
    from galley.letter import run_evidence

    run, ws = _evidence_run(tmp_path)
    cf = casefile_from_run(run)
    ev = run_evidence(run, ws)
    text = render_style_sheet(cf, tmp_path / "out", evidence=ev).read_text("utf-8")

    assert "No rulings were recorded" not in text
    assert "## Mechanical conventions" in text
    assert "| Lists |" in text and "| 1 |" in text
    # names, not vocabulary
    assert "Katena" in text
    assert "bronchoscopy" not in text and "IL" not in text.split("Names")[1][:200]
    # a spelling question is listed as pending, a date question is not
    assert "## Unresolved, pending the author" in text
    assert "Rodewall or Rodewell" in text
    assert "Thirteen years" not in text
    assert "comment ceiling for this book was 9" in text


def test_the_verification_report_names_what_it_checked(tmp_path):
    from galley.casefile_synth import casefile_from_run
    from galley.letter import render_verification_report, run_evidence

    run, ws = _evidence_run(tmp_path)
    cf = casefile_from_run(run)
    ev = run_evidence(run, ws)
    path = render_verification_report(ev, tmp_path / "out", cf=cf)
    assert path.name == "verification.md"
    text = path.read_text("utf-8")

    assert "| source hash | PASS |" in text
    assert "| intent-zone collisions | SKIP |" in text
    assert "did not run 1 check(s)" in text
    assert "**Result: passed, zero failing checks.**" in text
    assert "| Applied corrections re-read in context | 2 read; 1 flagged; all settled |" in text
    assert "9 paragraph(s) read; 2 residual(s); 0 unread" in text
    assert "2 round(s)" in text
    # no deliverable in this run: said, not invented
    assert "No tracked-changes .docx" in text
    assert "## Untracked preparation" in text
    assert "statistical estimate" in text


def test_render_all_without_evidence_keeps_the_ledger_letter(tmp_path):
    cf, ms = _scripted_casefile()
    letter, style = render_all(cf, tmp_path, ms=ms)
    text = letter.read_text("utf-8")
    assert "# Editorial letter" in text and "## Open queries" in text
    assert "Proofreading letter" not in text
