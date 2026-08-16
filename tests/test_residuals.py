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


def test_the_reports_own_misses_are_all_caught():
    found = q(["Only 1% rise up while the other 99% watch.",
               "It took 17 minutes and then 8 seconds more.",
               "By the 10th attempt he stopped counting."])
    texts = " | ".join(f.explanation for f in found)
    assert "one percent" in texts
    assert "ninety-nine percent" in texts
    assert "“seventeen” for “17”" in texts
    assert "“eight” for “8”" in texts
    assert "“tenth” for “10th”" in texts
    assert all(f.force_query and f.corrected_text == f.original_text
               for f in found)


def test_a_site_a_validated_edit_touches_is_not_residue():
    p = para("It took 17 minutes to land.")
    start = p.text.index("17")
    fixed = Finding("f-1", "c", p.para_id, "number_style", p.text, 1,
                    "", "", "high", status="validated",
                    anchor=Anchor(start, start + 2, "17", "seventeen"))
    assert q([p], validated=[fixed]) == []


def test_what_the_patterns_must_not_flag():
    found = q(["The 1,200 survivors left in 1969 at 8:30 with 3.5 tons.",
               "A 9mm round from a 5-year-old rifle at 250 yards.",
               "He woke at 6 a.m. and it was already 90° outside.",
               "Route 66's fame survived it."])   # digits glued to a word: 66's
    assert found == []


def test_headings_keep_their_numerals():
    assert q([para("Chapter 10", reviewable=False)]) == []


def test_the_cap_is_loud_not_silent(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="docproof.residuals")
    texts = [f"There were {n} of them again that day." for n in range(2, 30)]
    found = q(texts, max_per_rule=5)
    assert len(found) == 5
    assert any("cap" in r.message for r in caplog.records)
