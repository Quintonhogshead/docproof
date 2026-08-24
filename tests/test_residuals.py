"""Residual coverage for the number rules (DP-002): a rule applied to
some-but-not-all of its matches must end the run visible, not silently
partial.
"""
from __future__ import annotations

from docproof.models import Anchor, Finding, ParagraphRef
from docproof.residuals import residual_queries


def para(text: str, pid: str = "body-0000", reviewable: bool = True):
    return ParagraphRef(pid, "word/document.xml", "body", text, "Normal",
                        reviewable=reviewable)


def q(texts_or_paras, validated=(), **kw):
    ps = [para(t, f"body-{i:04d}") if isinstance(t, str) else t
          for i, t in enumerate(texts_or_paras)]
    return residual_queries(ps, list(validated), **kw)


def test_percent_and_ordinal_misses_are_still_queried():
    """The percent and ordinal rules stay questions — those conversions are
    judgment calls more often than the bare cardinal — so a site the pass left
    as digits still ends the run accounted for in the margin."""
    found = q(["Only 1% rise up while the other 99% watch.",
               "By the 10th attempt he stopped counting."])
    queries = [f for f in found if f.force_query]
    texts = " | ".join(f.explanation for f in queries)
    assert "one percent" in texts
    assert "ninety-nine percent" in texts
    assert "“tenth” for “10th”" in texts
    assert all(f.corrected_text == f.original_text for f in queries)


def test_a_clear_prose_numeral_is_converted_not_queried():
    """A bare cardinal counted by a plain noun is unambiguous prose, so the
    residual pass spells it out as a silent tracked change rather than asking."""
    found = q(["It took 17 minutes and then 8 seconds more."])
    assert found, "the prose numerals should have produced conversions"
    assert all(not f.force_query and f.silent and f.confidence == "high"
               for f in found)
    corrected = " ".join(f.corrected_text for f in found)
    assert "seventeen minutes" in corrected
    assert "eight seconds" in corrected


def test_a_judgment_case_stays_a_query():
    """A numeral that is a label, a measurement, or sentence-initial is left as
    digits and raised as a question, exactly as before."""
    found = q(["He walked down Highway 42 to the coast.",   # label
               "The rope was 9 mm thick.",                  # measurement
               "15 geese crossed the road."])               # sentence-initial
    assert found, "the judgment cases should have produced queries"
    assert all(f.force_query and f.corrected_text == f.original_text
               for f in found)


def test_a_site_a_validated_edit_touches_is_not_residue():
    p = para("It took 17 minutes to land.")
    start = p.text.index("17")
    fixed = Finding("f-1", "c", p.para_id, "number_style", p.text, 1,
                    "", "", "high", status="validated",
                    anchor=Anchor(start, start + 2, "17", "seventeen"))
    assert q([p], validated=[fixed]) == []


def test_a_site_a_gate_withheld_or_queried_finding_covers_is_left_alone():
    """A span a gate deliberately left as a question — a withheld edit, or a
    query — is spoken for. The residual pass must not convert it: doing so would
    re-apply what a gate declined and second-guess a question already raised."""
    p = para("It took 17 minutes to land.")
    start = p.text.index("17")
    for status in ("query", "skipped_low_confidence", "rejected_oversized"):
        held = Finding("h-1", "c", p.para_id, "number_style", p.text, 1,
                       "", "", "high", status=status,
                       anchor=Anchor(start, start + 2, "17", ""),
                       withheld=(status == "query"))
        assert q([p], validated=[held]) == [], f"{status} should suppress residue"


def test_an_ordinal_is_never_half_converted():
    """A number with a suffix — "2nd", "86th" — must never have its digits spelled
    out while the suffix is left ("twond"). The cardinal apply pass excludes any
    digit glued to letters, and the ordinal rule only ever queries, spelling the
    whole word in its suggestion."""
    found = q(["She came 2nd, and he was 86th out of the pack.",
               "A 5-year-old rifle and a 9mm round lay in the 3rd drawer."])
    # Nothing here is applied: ordinals stay questions, glued numbers untouched.
    assert all(f.force_query and f.corrected_text == f.original_text
               for f in found)
    texts = " | ".join(f.explanation for f in found)
    assert "“second”" in texts and "“eighty-sixth”" in texts and "“third”" in texts


def test_what_the_patterns_must_not_flag():
    found = q(["The 1,200 survivors left in 1969 at 8:30 with 3.5 tons.",
               "A 9mm round from a 5-year-old rifle at 250 yards.",
               "He woke at 6 a.m. and it was already 90° outside.",
               "Route 66's fame survived it."])   # digits glued to a word: 66's
    assert found == []


def test_headings_keep_their_numerals():
    assert q([para("Chapter 10", reviewable=False)]) == []


def test_page_furniture_numerals_are_left_alone():
    # A folio in a footer ("2") and a running head are typesetting, not prose:
    # the number rules must not query them, even though the paragraph is
    # reviewable. (A footer page number was the residual pass's one false query.)
    footer = ParagraphRef("footer3-p0", "word/footer3.xml", "footer", "2",
                          "Normal", reviewable=True)
    header = ParagraphRef("header2-p0", "word/header2.xml", "header",
                          "Chapter 3", "Normal", reviewable=True)
    assert q([footer, header]) == []


def test_the_query_cap_is_loud_not_silent(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="docproof.residuals")
    # "Room N" is a label, so each stays a query — the capped channel. (A clear
    # prose conversion is never capped, since every one is a change that lands.)
    texts = [f"Room {n} stayed empty that winter." for n in range(2, 30)]
    found = q(texts, max_per_rule=5)
    assert len(found) == 5
    assert all(f.force_query for f in found)
    assert any("cap" in r.message for r in caplog.records)


def test_clear_prose_conversions_are_never_capped(caplog):
    """The cap guards the query channel against flooding the margin; an applied
    conversion is a change that lands, so every one is kept."""
    texts = [f"She saw {n} birds that morning." for n in range(2, 30)]
    found = q(texts, max_per_rule=5)
    assert len(found) == len(texts)
    assert all(not f.force_query and f.silent for f in found)
