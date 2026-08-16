"""The external-world fact check (DP-004): one whole-book read, and every
catch a question — never an edit, because fiction bends the world on purpose.
"""
from __future__ import annotations

from docproof.factcheck import (FactReport, FactSuspect, build_factcheck,
                                suspect_queries)
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def test_build_parses_a_report():
    prov = FakeProvider([ProviderResult(parsed={"suspects": [
        {"quote": "National Traffic Safety Board", "kind": "acronym",
         "issue": "The NTSB is not expanded this way.",
         "accepted": "National Transportation Safety Board."}]})])
    r = build_factcheck([_para("body-0", "x")], prov, model="m",
                        max_tokens=100, usage=Usage())
    assert r.suspects[0].kind == "acronym"


def test_build_degrades_to_empty_on_failure():
    prov = FakeProvider([ProviderResult(parsed=None, stop_reason="max_tokens")])
    r = build_factcheck([_para("body-0", "x")], prov, model="m",
                        max_tokens=100, usage=Usage())
    assert r.suspects == []


def test_the_reports_own_misses_become_anchored_queries():
    ps = [_para("body-0000",
                "The National Traffic Safety Board opened its inquiry."),
          _para("body-0001",
                "Universal Base Income had carried the vote by then.")]
    report = FactReport(suspects=[
        FactSuspect(quote="National Traffic Safety Board",
                    issue="The NTSB expands differently.",
                    accepted="National Transportation Safety Board.",
                    kind="acronym"),
        FactSuspect(quote="Universal Base Income",
                    issue="The program's accepted name differs.",
                    accepted="Universal Basic Income.", kind="institution")])
    found = suspect_queries(report, ps)
    assert [f.para_id for f in found] == ["body-0000", "body-0001"]
    assert all(f.force_query for f in found)
    assert all(f.corrected_text == f.original_text for f in found)
    assert "question only" in found[0].explanation


def test_a_paraphrased_quote_is_dropped_not_misplaced():
    ps = [_para("body-0000", "The Board opened its inquiry.")]
    report = FactReport(suspects=[
        FactSuspect(quote="something the model made up", issue="x",
                    accepted="y", kind="other")])
    assert suspect_queries(report, ps) == []


def test_curly_punctuation_does_not_defeat_the_anchor():
    ps = [_para("body-0000", "Cortez’s march across South America began.")]
    report = FactReport(suspects=[
        FactSuspect(quote="Cortez's march across South America",
                    issue="Cortés conquered Mexico, not South America.",
                    accepted="Cortés, in Mexico.", kind="history")])
    (f,) = suspect_queries(report, ps)
    # The anchor is the manuscript's own characters, not the model's quote.
    assert f.original_text == "Cortez’s march across South America"


def test_the_query_cap_is_enforced():
    ps = [_para(f"body-{i:04d}", f"Fact number {i} stands here.")
          for i in range(5)]
    report = FactReport(suspects=[
        FactSuspect(quote=f"Fact number {i}", issue="x", accepted="y",
                    kind="other") for i in range(5)])
    assert len(suspect_queries(report, ps, max_queries=2)) == 2
