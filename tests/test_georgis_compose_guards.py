"""The four composition guards from the Georgis Book 1 run (2026-09-04),
found while `import-findings` composed rows from several lanes into one
build: a mid-word anchor, adjacent same-seam insertions, a recurrence pass
seeded from a chapter-sweep composite, and a fix that ships a doubled word.
Every string here is the one from runs/HARNESS_NOTES.md.
"""
from __future__ import annotations

import json

import pytest

from docproof.__main__ import main
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from docproof.providers.base import ProviderResult
from docproof.validator import (anchors_midword, introduces_doubled_word,
                                validate_findings, _overlaps)

from .conftest import FIXTURES


def _doc(*texts: str) -> DocumentModel:
    return DocumentModel(
        source_path="x.docx",
        paragraphs=tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml",
                                      "body", t, "Normal")
                         for i, t in enumerate(texts)))


def _f(fid: str, original: str, corrected: str, *, para: str = "body-0000",
       etype: str = "imported_edit", occurrence: int = 1) -> Finding:
    return Finding(fid, "chunk-000", para, etype, original, occurrence,
                   corrected, "x", "high")


# --- (8) anchoring is word-boundary-aware at the edge the edit touches -------

SOURCE_BORNE = "It was a friendship our bond borne of long winters together."


def test_a_quote_cut_mid_word_at_the_edited_edge_does_not_anchor():
    """'our bond born' is a plain substring of 'our bond borne'; the edit
    reaches the cut word, so applying it would splice inside 'borne'."""
    doc = _doc(SOURCE_BORNE)
    out = validate_findings(
        [_f("i-1", "our bond born", "our bond was born")], doc, "medium")
    assert out[0].status == "rejected_anchor_midword"
    assert "borne" in doc.paragraphs[0].text          # untouched


def test_a_mid_word_cut_the_edit_does_not_reach_still_anchors():
    doc = _doc(SOURCE_BORNE)
    # The quote still ends inside 'borne', but the edit is on 'friendship'.
    out = validate_findings(
        [_f("i-1", "a friendship our bond born", "a friendship, our bond born")],
        doc, "medium")
    assert out[0].status == "validated"
    assert doc.paragraphs[0].text[out[0].anchor.start:out[0].anchor.end] == ""
    assert out[0].anchor.insert_text == ","


def test_anchors_midword_covers_both_edges_and_apostrophes():
    text = "the apple’s skin was green"
    # quote 'apple' before '’s': the edit touches 'apple' -> mid-word
    assert anchors_midword(text, 4, "apple", 0, "apple")
    # quote starting inside 'green' ('reen'), edit at its start
    assert anchors_midword(text, text.index("reen"), "reen", 0, "reen")
    # a whole-word quote is never mid-word
    assert not anchors_midword(text, 4, "apple’s", 0, "apple")


# --- (9) same-seam insertions are an overlap; the build iterates to clean ----

def test_two_insertions_one_char_apart_overlap():
    assert _overlaps(10, 10, 10, 10)
    assert _overlaps(10, 10, 11, 11)
    assert _overlaps(11, 11, 10, 10)
    assert not _overlaps(10, 10, 12, 12)
    # an insertion abutting a span's END still composes cleanly ("color,")
    assert not _overlaps(3, 5, 5, 5)


def test_a_second_comma_at_the_same_seam_loses_to_the_first_lane():
    """The ladder, the fleet and settle each inserted a comma after 'luck';
    two rows with the comma one char apart composed ',,'. Earliest lane
    wins; the later row is rejected as an overlap."""
    src = "With any luck they would be home by dark."
    doc = _doc(src)
    ladder = _f("m-1", "any luck they", "any luck, they", etype="comma_splice")
    fleet = _f("i-2", "luck they would", "luck ,they would")   # a char later
    out = validate_findings([ladder, fleet], doc, "medium")
    st = {f.finding_id: f.status for f in out}
    # imported rows are promoted ahead of machine rows, so the fleet's comma
    # lands first and the ladder's is the overlap — either way exactly ONE
    # comma reaches the seam.
    assert sorted(st.values()) == ["rejected_overlap", "validated"]


def test_the_time_sweep_and_the_number_audit_do_not_both_insert_00():
    src = "The shift ran from 7 AM until noon."
    doc = _doc(src)
    sweep = _f("s-1", "from 7 AM until", "from 7:00 AM until",
               etype="sweep_time_of_day")
    audit = _f("i-2", "7 AM", "7 :00 AM")
    out = validate_findings([sweep, audit], doc, "medium")
    assert sorted(f.status for f in out) == ["rejected_overlap", "validated"]


BODY_1 = "Although it was late, we kept driving toward the coast."


def test_import_findings_drops_the_later_row_when_the_build_composes_an_artifact(
        tmp_path, capsys):
    """Two rows that each pass the overlap check — a ',' -> ';' replacement
    and a ', ' inserted just past it — compose 'late; , we'. The scan after
    validation catches it and the LATER row loses."""
    findings = tmp_path / "rows.json"
    findings.write_text(json.dumps([
        {"para_id": "body-0001", "original_text": "late, we",
         "corrected_text": "late; we", "error_type": "sweep_dash"},
        {"para_id": "body-0001", "original_text": "we kept",
         "corrected_text": ", we kept"},
    ]), encoding="utf-8")
    out = tmp_path / "out"
    rc = main(["import-findings", str(findings), str(FIXTURES / "simple.docx"),
               "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "artifact scan:" in printed and "dropped the later row" in printed
    payload = json.loads((out / "findings.json").read_text("utf-8"))
    rows = payload["findings"]
    landed = [r for r in rows if r.get("applied")]
    assert len(landed) == 1
    assert landed[0]["corrected_text"] == "late; we"
    from galley.verify import paragraph_views
    acc = paragraph_views(out)[1]["body-0001"]
    assert acc == BODY_1.replace("late, we", "late; we")
    assert " , " not in acc


# --- (10) recurrence never seeds from a chapter-sweep row or a degenerate fix -

spylls = pytest.importorskip("spylls", reason="the dictionary needs spylls")


def _paras(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def _seed(para: ParagraphRef, surface: str, fix: str, *,
          error_type: str = "spelling", chunk_id: str = "chunk-000") -> Finding:
    start = para.text.find(surface)
    end = start + len(surface)
    return Finding(
        finding_id="seed", chunk_id=chunk_id, para_id=para.para_id,
        error_type=error_type, original_text=para.text, occurrence=1,
        corrected_text=para.text[:start] + fix + para.text[end:],
        explanation="", confidence="high", status="validated",
        anchor=Anchor(start=start, end=end, delete_text=surface,
                      insert_text=fix))


def test_a_chapter_sweep_row_never_seeds_propagation():
    from docproof.adjudicate import propagate_recurrences
    ps = _paras("We would gather the tools and them.",
                "She counted the tools and them.",
                "He put the tools and them away.")
    seed = _seed(ps[0], "and them", "and use them", error_type="chapter_sweep",
                 chunk_id="chapter_sweep")
    assert propagate_recurrences([seed], ps) == []
    # typed `spelling` but from the chapter-sweep chunk: still never a seed
    seed2 = _seed(ps[0], "and them", "and use them", chunk_id="chapter_sweep")
    assert propagate_recurrences([seed2], ps) == []


def test_a_fix_longer_than_two_words_or_a_function_word_surface_is_degenerate(
        caplog):
    from docproof.adjudicate import propagate_recurrences
    ps = _paras("We would gather the tools and them.",
                "She counted the tools and them.",
                "He put the tools and them away.")
    with caplog.at_level("INFO"):
        # "and use them" (three words) from an ordinary typed pass
        assert propagate_recurrences(
            [_seed(ps[0], "and them", "and use them")], ps) == []
        # a function-word surface ("them" -> "these") from a typed pass
        assert propagate_recurrences(
            [_seed(ps[0], "them", "these")], ps) == []
    assert sum("degenerate seed" in r.message for r in caplog.records) == 2
    # a genuine two-word fix still seeds
    ps2 = _paras("She staired at him.", "He staired back.")
    out = propagate_recurrences([_seed(ps2[0], "staired", "stared")], ps2)
    assert [f.para_id for f in out] == ["body-0001"]


# --- (11) a fix that ships a doubled word is refused ------------------------

def test_introduces_doubled_word_names_the_georgis_doubling():
    assert introduces_doubled_word("we sat and, later, left",
                                   "we sat and and, later, left") == "and"
    assert introduces_doubled_word("that that was it", "that that was it") is None
    assert introduces_doubled_word("we so left", "we so so left") == "so"


def test_the_validator_refuses_a_correction_that_doubles_a_word():
    src = "They packed the wagons and, at first light, set out."
    doc = _doc(src)
    out = validate_findings(
        [_f("c-1", "the wagons and, at", "the wagons and and, at",
            etype="chapter_sweep")], doc, "medium")
    assert out[0].status == "rejected_doubled_word_in_fix"
    # the same words without the doubling still land
    out = validate_findings(
        [_f("c-2", "the wagons and, at", "the wagons and at", etype="chapter_sweep")],
        doc, "medium")
    assert out[0].status == "validated"


def test_the_chapter_sweep_drops_a_doubled_word_proposal_before_confirm():
    from docproof import chaptersweep
    from docproof.models import Usage

    class _Prov:
        def complete_structured(self, **kw):
            return ProviderResult(parsed={"findings": [
                {"para": 1, "quote": "the wagons and, at",
                 "correction": "the wagons and and, at", "note": "x"},
                {"para": 1, "quote": "set out", "correction": "set off",
                 "note": "y"}]}, stop_reason="ok")
    paras = _paras("They packed the wagons and, at first light, set out.")
    stats: dict = {}
    out = chaptersweep.propose(paras, _Prov(), model="m", max_output_tokens=10,
                               usage=Usage(), window_chars=10_000, stats=stats)
    assert [c.replacement for c in out] == ["set off"]
    assert stats["doubled_word"] == 1
