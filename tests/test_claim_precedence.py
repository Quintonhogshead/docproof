"""Claim precedence on contested spans (P0-2, Purpura beta run 3).

`validate_findings` is first-come-wins on any span two edits both touch, so the
input order IS the precedence. The caller's order already encodes the machine
hierarchy (sweeps > consistency > repair > model …); what it did NOT encode is
that a HAND-MADE row — a curated or replayed human edit — must outrank a machine
floor/model row it contends with. On the Purpura replay a junk floor row beat a
curated fix (terd->turd lost to terd->nerd) purely because it was listed first.

These tests pin the fix: an imported/replayed row claims a contested span ahead
of any machine row, the minimal one wins a tie between two human rows, and a run
with no imported rows is reordered by nothing.
"""
from __future__ import annotations

from docproof.models import DocumentModel, Finding, ParagraphRef
from docproof.validator import validate_findings


def _doc(text: str) -> DocumentModel:
    para = ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal")
    return DocumentModel(source_path="x.docx", paragraphs=(para,))


def _finding(fid: str, original: str, corrected: str, *,
             error_type: str = "spelling", confidence: str = "high") -> Finding:
    return Finding(finding_id=fid, chunk_id="c", para_id="body-0000",
                   error_type=error_type, original_text=original, occurrence=1,
                   corrected_text=corrected, explanation="x", confidence=confidence)


def _validated(out):
    return {(f.anchor.start, f.anchor.end, f.anchor.insert_text)
            for f in out if f.status == "validated"}


def test_curated_row_beats_a_floor_row_on_the_same_span():
    # Two edits on the SAME character: a junk floor row (cat->bat) listed FIRST,
    # a curated import row (cat->rat) listed second. Without precedence the floor
    # row wins by order; with it the human row does.
    doc = _doc("the cat sat")                       # cat at 4:7, the 'c' at 4
    floor = _finding("lt-0001", "cat", "bat")        # machine floor, first
    curated = _finding("import-0007", "cat", "rat")   # human, second
    out = validate_findings([floor, curated], doc, "medium")
    winners = _validated(out)
    assert (4, 5, "r") in winners              # the curated fix landed
    assert (4, 5, "b") not in winners          # the floor row was overlapped out
    (loser,) = [f for f in out if f.finding_id == "lt-0001"]
    assert loser.status == "rejected_overlap"


def test_no_imported_rows_leaves_order_untouched():
    # Two machine rows contend on the same character; the FIRST-listed still wins,
    # exactly as before — the precedence sort must not reshuffle a run that has no
    # imported rows.
    doc = _doc("the cat sat")
    first = _finding("s-0001", "cat", "bat")
    second = _finding("m-0002", "cat", "rat")
    out = validate_findings([first, second], doc, "medium")
    assert (4, 5, "b") in _validated(out)          # caller order preserved
    assert (4, 5, "r") not in _validated(out)


def test_minimal_span_wins_a_tie_between_two_human_rows():
    # Two imported rows contend on overlapping spans: a wide one quoting the
    # whole phrase and a minimal one quoting just the fixed word. The minimal
    # row claims first.
    doc = _doc("a rap sheet entry")
    wide = _finding("import-0001", "a rap sheet entry", "a rap-sheet entry")
    minimal = _finding("import-0002", "rap sheet", "rap-sheet")
    out = validate_findings([wide, minimal], doc, "medium")
    kept = [f for f in out if f.status == "validated"]
    # The minimal row won; the wide row was overlapped out.
    assert any(f.finding_id == "import-0002" for f in kept)
    assert all(f.finding_id != "import-0001" for f in kept)


def test_a_query_never_evicts_an_edit_on_the_same_span():
    # A consistency QUERY and a curated EDIT touch the same characters. A query
    # claims no span (it is a margin comment), so both survive — the edit is not
    # lost to the question.
    doc = _doc("she wore a sweat stache")
    query = _finding("s-0001", "sweat stache", "sweat stache",
                     error_type="term_consistency")
    query = query.__class__(**{**query.__dict__, "force_query": True})
    edit = _finding("import-0003", "sweat stache", "sweat-stache")
    out = validate_findings([query, edit], doc, "medium",
                            query_types=frozenset({"term_consistency"}))
    statuses = {f.finding_id: f.status for f in out}
    assert statuses["import-0003"] == "validated"      # the edit survived
    assert statuses["s-0001"] == "query"               # the question survived too
