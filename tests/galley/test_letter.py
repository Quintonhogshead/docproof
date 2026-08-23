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
