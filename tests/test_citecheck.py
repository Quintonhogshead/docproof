"""The citation/cross-reference referee: what it flags, and — since a false
"unresolved" query wastes author attention — everything it must leave alone.
"""
from __future__ import annotations

import itertools
import json

from docproof.citecheck import check, render
from docproof.models import ParagraphRef


def _doc(*items):
    """Each item is a text (Normal style) or a (text, style) pair."""
    paras = []
    for i, item in enumerate(items):
        text, style = item if isinstance(item, tuple) else (item, "Normal")
        paras.append(ParagraphRef(f"body-{i:04d}", "word/document.xml",
                                  "body", text, style))
    return paras


REFS = [
    ("References", "Heading1"),
    "Smith, J. (2020). The shape of rivers. Delta Press.",
    "Jones, A. (2019). Field methods for hydrology. Meadow Books.",
    "Nakamura, H., Lee, S., & Ortiz, P. (2017). Sensor arrays. TechHouse.",
]


def _kinds(report, kind):
    return [i for i in report.issues if i.kind == kind]


# --- citations that resolve ---------------------------------------------------

def test_matched_citations_produce_no_issues():
    r = check(_doc(
        "Rivers braid where the gradient collapses (Smith, 2020).",
        "Jones (2019) measured the same reach directly in the field.",
        "Later instrument work confirmed the pattern (Nakamura et al., 2017).",
        *REFS))
    assert r.has_references is True
    assert r.reference_entries == 3
    assert r.in_text_citations == 3
    assert r.issues == ()


def test_multiple_refs_in_one_paren_split_on_semicolons():
    r = check(_doc(
        "The result is well established (Smith, 2020; Jones, 2019; "
        "Nakamura et al., 2017).",
        *REFS))
    assert r.in_text_citations == 3
    assert r.issues == ()


# --- an in-text cite with no entry --------------------------------------------

def test_unmatched_citation_reported_once_even_when_cited_three_times():
    r = check(_doc(
        "The claim originates elsewhere (Nguyen, 2001).",
        "Nguyen (2001) framed it as a scaling law.",
        "It has been repeated since (see Nguyen, 2001).",
        "Local work agrees (Smith, 2020; Jones, 2019; Nakamura et al., 2017).",
        *REFS))
    hits = _kinds(r, "unmatched_citation")
    assert len(hits) == 1
    assert len(r.issues) == 1            # nothing else is wrong with this book
    issue = hits[0]
    assert issue.para_id == "body-0000"  # first occurrence
    assert "Nguyen" in issue.text
    assert "3x" in issue.detail          # total occurrence count
    assert "2001" in issue.detail


# --- an entry nobody cites ----------------------------------------------------

def test_uncited_entry_is_reported_at_its_own_paragraph():
    r = check(_doc(
        "Braiding follows the gradient (Smith, 2020).",
        "Jones (2019) provides the field protocol.",
        *REFS))
    hits = _kinds(r, "unmatched_entry")
    assert len(hits) == 1
    assert hits[0].text.startswith("Nakamura")
    assert hits[0].para_id == "body-0005"   # the entry's own paragraph
    assert "never cited" in hits[0].detail


# --- no reference list = auto-skip, not an error ------------------------------

def test_no_reference_section_means_no_citation_issues():
    r = check(_doc(
        "The claim is widely repeated (Nguyen, 2001).",
        "Smith (2020) said otherwise, at length.",
        "A closing paragraph with no citations at all."))
    assert r.has_references is False
    assert r.in_text_citations == 2      # still counted for the report
    assert r.issues == ()


def test_a_heading_with_too_few_entries_is_not_a_reference_list():
    r = check(_doc(
        "An orphan citation (Nguyen, 2001).",
        ("References", "Heading1"),
        "Smith, J. (2020). The only entry. Delta Press."))
    assert r.has_references is False
    assert r.issues == ()


# --- chapter cross-references -------------------------------------------------

def test_reference_past_the_last_chapter_is_flagged():
    r = check(_doc(
        ("Chapter One", "Heading1"), "Opening prose of the first chapter.",
        ("Chapter 2", "Heading1"), "Second chapter prose.",
        ("Chapter Three", "Heading1"), "Third.",
        ("Chapter 4", "Heading1"), "Fourth.",
        ("Chapter Five", "Heading1"), "Fifth.",
        ("Chapter Six", "Heading1"),
        "For the full derivation, see chapter 9.",
        "We already touched on this in chapter three."))
    assert r.chapters_found == 6
    hits = _kinds(r, "chapter_ref")
    assert len(hits) == 1                # "chapter three" resolved fine
    assert "chapter 9" in hits[0].text
    assert "chapter 6" in hits[0].detail


def test_spelled_out_chapter_reference_resolves():
    r = check(_doc(
        ("Chapter One", "Heading1"), "First.",
        ("Chapter Two", "Heading1"),
        "As argued in chapter one, the premise holds."))
    assert r.issues == ()


def test_no_chapter_headings_means_no_chapter_checking():
    r = check(_doc(
        "A book of essays with unnumbered sections.",
        "The author still writes: see chapter 9 of Gibbon."))
    assert r.chapters_found == 0
    assert r.issues == ()


# --- figure and table cross-references ----------------------------------------

def test_reference_to_an_uncaptioned_figure_is_flagged():
    r = check(_doc(
        "Figure 1. Map of the delta.",
        "Figure 2. Discharge over time.",
        "Figure 3. Sediment load.",
        "The anomaly is unmistakable (see Figure 4).",
        "Compare Figure 2 for the seasonal baseline."))
    assert r.figures_found == 3
    hits = _kinds(r, "figure_ref")
    assert len(hits) == 1                # Figure 2 resolved
    assert "Figure 4" in hits[0].text
    assert "no Figure 4 caption" in hits[0].detail


def test_zero_captions_means_figure_refs_are_skipped():
    r = check(_doc(
        "The book has no labeled figures at all.",
        "And yet the prose says: see Figure 4 for details."))
    assert r.figures_found == 0
    assert r.issues == ()


def test_table_refs_use_their_own_kind():
    r = check(_doc(
        "Table 1. Gauging stations.",
        "Table 2. Annual peaks.",
        "Table 3. Flood stages.",
        "The outliers appear in Table 7."))
    hits = _kinds(r, "table_ref")
    assert len(hits) == 1
    assert hits[0].kind == "table_ref"
    assert "Table 7" in hits[0].text


# --- et al. matches by first author -------------------------------------------

def test_et_al_matches_entry_by_first_author_and_year():
    r = check(_doc(
        "Arrays of this kind are described elsewhere (Nakamura et al., 2017).",
        "Nakamura et al. (2017) also report the failure mode.",
        "The rest of the literature agrees (Smith, 2020; Jones, 2019).",
        *REFS))
    assert r.in_text_citations == 4
    assert r.issues == ()


# --- the stop-list ------------------------------------------------------------

def test_stoplist_word_before_a_year_is_not_a_citation():
    r = check(_doc(
        "(See 2020) was scrawled in the margin of the manuscript.",
        "All three sources concur (Smith, 2020; Jones, 2019; "
        "Nakamura et al., 2017).",
        *REFS))
    assert r.in_text_citations == 3      # the "(See 2020)" never counted
    assert _kinds(r, "unmatched_citation") == []


# --- serialization ------------------------------------------------------------

def test_to_json_round_trips_through_json_dumps():
    r = check(_doc(
        "An orphan claim (Nguyen, 2001).",
        "The rest resolve (Smith, 2020; Jones, 2019; Nakamura et al., 2017).",
        *REFS))
    d = r.to_json()
    restored = json.loads(json.dumps(d))
    assert restored == d
    assert restored["has_references"] is True
    assert restored["reference_entries"] == 3
    assert restored["issues"][0]["kind"] == "unmatched_citation"
    assert set(restored["issues"][0]) == {"kind", "para_id", "text", "detail"}


# --- render -------------------------------------------------------------------

def test_render_names_the_kinds_present():
    out = render(check(_doc(
        ("Chapter One", "Heading1"),
        "Figure 1. A lone caption.",
        "An orphan citation (Nguyen, 2001), a dangling see chapter 9, "
        "and a missing plot (see Figure 4).",
        "The good citations (Smith, 2020; Jones, 2019).",
        *REFS)))
    assert "unmatched_citation" in out
    assert "unmatched_entry" in out      # Nakamura is never cited
    assert "chapter_ref" in out
    assert "figure_ref" in out
    assert "report-only" in out


def test_render_caps_issue_lines_at_forty():
    surnames = ["".join(p).capitalize() + "son"
                for p in itertools.product("abcdefg", repeat=2)][:45]
    entries = [f"{name}, A. ({1950 + i}). Collected works. Press."
               for i, name in enumerate(surnames)]
    out = render(check(_doc(
        "A body paragraph that cites nothing at all.",
        ("References", "Heading1"),
        *entries)))
    issue_lines = [l for l in out.splitlines() if l.startswith("  body-")]
    assert len(issue_lines) == 40
    assert "+5 more" in out
