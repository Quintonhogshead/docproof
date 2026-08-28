"""The contents-vs-body check: one small structure-extract read, and every
catch a question — never an edit, because which copy is right (the contents
or the chapter page) is the author's to settle. The Purpura miss it exists
for: "so massive that" in the contents, "so drastic that" on the part page.
"""
from __future__ import annotations

from docproof.config import Config
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult
from docproof.toccheck import (TocReport, TocSuspect, build_toccheck,
                               structure_extract, suspect_queries)

from .fakes import FakeProvider


def _para(pid: str, text: str, style: str = "Normal") -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style=style)


def _purpura_shape() -> list[ParagraphRef]:
    """A miniature of the Purpura front matter and body."""
    return [
        _para("body-0000", "PART 2"),
        _para("body-0001", "THE INFERNO"),
        _para("body-0002", "Change can be so massive that it blows everything up."),
        _para("body-0003", "5. An explosion"),
        _para("body-0004", "A firestorm ignites"),
        _para("body-0005", "PREFACE", style="Heading1"),
        _para("body-0006", "Dear my forties, I never gave you much thought. "
                           "You were merely the decade after my thirties, and "
                           "nothing about you seemed worth dreading then."),
        _para("body-0007", "PART 2", style="Heading1"),
        _para("body-0008", "THE INFERNO"),
        _para("body-0009", "Change can be so drastic that it blows everything up."),
    ]


def test_structure_extract_carries_front_matter_and_outline():
    extract = structure_extract(_purpura_shape(), Config().skip)
    assert "[OPENING PAGES]" in extract and "[BODY OUTLINE]" in extract
    # The contents-side epigraph rides the opening pages verbatim.
    assert "so massive that" in extract
    # The body-side heading and its epigraph zone ride the outline.
    assert "H: PREFACE" in extract
    assert "~ Change can be so drastic" in extract


def test_extract_declines_without_any_headings():
    ps = [_para("body-0000", "Just a paragraph of ordinary prose here.")]
    prov = FakeProvider([])
    r = build_toccheck(ps, prov, skip=Config().skip, model="m",
                       max_tokens=100, usage=Usage())
    assert r.suspects == []            # no structure to compare — no call made


def test_build_parses_a_report():
    prov = FakeProvider([ProviderResult(parsed={"suspects": [
        {"quote": "Change can be so drastic that it blows everything up.",
         "issue": "The contents reads 'massive'; the part page reads 'drastic'.",
         "counterpart": "Change can be so massive that it blows everything up.",
         "kind": "wording"}]})])
    r = build_toccheck(_purpura_shape(), prov, skip=Config().skip, model="m",
                       max_tokens=100, usage=Usage())
    assert r.suspects[0].kind == "wording"


def test_build_degrades_to_empty_on_failure():
    prov = FakeProvider([ProviderResult(parsed=None, stop_reason="max_tokens")])
    r = build_toccheck(_purpura_shape(), prov, skip=Config().skip, model="m",
                       max_tokens=100, usage=Usage())
    assert r.suspects == []


def test_a_mismatch_becomes_an_anchored_query_naming_both_copies():
    ps = _purpura_shape()
    report = TocReport(suspects=[TocSuspect(
        quote="Change can be so drastic that it blows everything up.",
        issue="The contents reads 'massive'; the part page reads 'drastic'.",
        counterpart="Change can be so massive that it blows everything up.",
        kind="wording")])
    (f,) = suspect_queries(report, ps)
    assert f.para_id == "body-0002" or f.para_id == "body-0009"
    assert f.force_query and f.corrected_text == f.original_text
    assert "massive" in f.explanation and "drastic" in f.explanation
    assert "question only" in f.explanation


def test_a_paraphrased_quote_is_dropped_not_misplaced():
    report = TocReport(suspects=[TocSuspect(
        quote="a line the model invented", issue="x", counterpart="",
        kind="other")])
    assert suspect_queries(report, _purpura_shape()) == []


def test_the_query_cap_is_enforced():
    ps = [_para(f"body-{i:04d}", f"Chapter line number {i} stands here.")
          for i in range(6)]
    report = TocReport(suspects=[
        TocSuspect(quote=f"Chapter line number {i}", issue="x",
                   counterpart="", kind="numbering") for i in range(6)])
    assert len(suspect_queries(report, ps, max_queries=3)) == 3


def test_shipped_config_enables_the_pass_on_a_cheap_model():
    from app.settings import CONFIG_PATH
    from docproof.config import load_config
    cfg = load_config(CONFIG_PATH)
    assert cfg.toccheck.enabled
    assert cfg.toccheck.model == "gpt-5.6-luna"
