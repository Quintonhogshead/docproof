"""The whole-book glossary pass: parsing the model's read, and turning it into
adjudication candidates and query-channel case-drift findings."""
from __future__ import annotations

import itertools

from docproof.glossary import (Glossary, GlossaryEntry, Suspect, build_glossary,
                              case_drift_findings, suspects_to_candidates)
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


# --- build --------------------------------------------------------------------

def test_build_parses_a_glossary():
    prov = FakeProvider([ProviderResult(parsed={
        "entries": [{"canonical": "Upper City", "kind": "place",
                     "variants": ["Upper city"]}],
        "suspected_misspellings": [{"surface": "calvary", "likely": "cavalry"}]})])
    g = build_glossary([_para("body-0", "x")], prov, model="m",
                       max_tokens=100, usage=Usage())
    assert g.entries[0].canonical == "Upper City"
    assert g.suspected_misspellings[0].surface == "calvary"


def test_build_degrades_to_empty_on_failure():
    # A refusal, a truncation, or malformed output must not break the review.
    prov = FakeProvider([ProviderResult(stop_reason="refusal")])
    g = build_glossary([_para("body-0", "x")], prov, model="m",
                       max_tokens=100, usage=Usage())
    assert g.entries == [] and g.suspected_misspellings == []


# --- suspects -> candidates ---------------------------------------------------

def test_suspects_become_candidates_at_each_occurrence():
    g = Glossary(suspected_misspellings=[Suspect(surface="calvary", likely="cavalry")])
    paras = [_para("body-0", "A calvary regiment rode past."),
             _para("body-1", "The calvary charged at dawn.")]
    cands = suspects_to_candidates(g, paras)
    assert len(cands) == 2
    assert {c.para_id for c in cands} == {"body-0", "body-1"}
    assert all(c.suggestion == "cavalry" and c.kind == "glossary" for c in cands)
    c0 = next(c for c in cands if c.para_id == "body-0")
    assert paras[0].text[c0.start:c0.end] == "calvary"


def test_multiword_or_nonalpha_suspects_are_skipped():
    g = Glossary(suspected_misspellings=[
        Suspect(surface="respect witch", likely="respective witch"),  # phrase
        Suspect(surface="", likely="x")])
    assert suspects_to_candidates(g, [_para("body-0", "the respect witch")]) == []


def test_a_suspect_does_not_match_inside_a_longer_word():
    g = Glossary(suspected_misspellings=[Suspect(surface="acer", likely="acre")])
    # "lacerations" contains "acer" but must not be flagged.
    assert suspects_to_candidates(g, [_para("body-0", "the lacerations wept")]) == []


# --- case drift ---------------------------------------------------------------

def test_case_drift_discovers_a_split_proper_noun_without_a_listed_variant():
    # The scan finds the stray casing itself — the model need not have listed it
    # (it usually does not). A near-even split is asked about, never forced.
    g = Glossary(entries=[GlossaryEntry(canonical="Upper City", kind="place",
                                        variants=[])])
    paras = [_para("body-0", "The Upper City gleamed above them."),
             _para("body-1", "She climbed toward the upper city gate."),
             _para("body-2", "Guards patrolled the upper city wall.")]
    out = case_drift_findings(g, paras, itertools.count(1))
    assert len(out) == 2                             # both lowercase strays
    assert all(f.force_query for f in out)           # split -> ask
    assert all(f.error_type == "capitalization" for f in out)
    assert out[0].corrected_text == "She climbed toward the Upper City gate."


def test_case_drift_leaves_an_author_lowercased_phrase_alone():
    # The glossary over-catalogs; when the author writes a phrase lowercase
    # throughout, that IS the convention — a lone stray capital is not a mandate.
    g = Glossary(entries=[GlossaryEntry(canonical="Domino Cloak", kind="thing",
                                        variants=[])])
    paras = [_para(f"body-{i}", "He wore the domino cloak.") for i in range(9)]
    paras.append(_para("body-9", "The Domino Cloak shimmered."))
    assert case_drift_findings(g, paras, itertools.count(1)) == []


def test_case_drift_corrects_a_decisively_dominant_name():
    # When the proper styling owns the book, a stray is a tracked change.
    g = Glossary(entries=[GlossaryEntry(canonical="Prism Knight",
                                        kind="character", variants=[])])
    paras = [_para(f"body-{i}", "The Prism Knight bowed.") for i in range(10)]
    paras.append(_para("body-x", "So the prism knight bowed."))
    out = case_drift_findings(g, paras, itertools.count(1),
                              edit_dominance=5, edit_min_count=8)
    assert len(out) == 1
    assert out[0].force_query is False               # dominant -> tracked edit
    assert out[0].corrected_text == "So the Prism Knight bowed."


def test_case_drift_normalises_a_leading_article():
    # A "The Upper City" canonical must not flag every mid-sentence "the Upper
    # City" — only a content word's casing counts, never the article's.
    g = Glossary(entries=[GlossaryEntry(canonical="The Upper City", kind="place",
                                        variants=[])])
    paras = [_para("body-0", "The Upper City gleamed."),
             _para("body-1", "She reached the Upper City by noon."),
             _para("body-2", "Beyond lay the Upper City wall.")]
    assert case_drift_findings(g, paras, itertools.count(1)) == []


def test_case_drift_legacy_path_uses_model_variants():
    # With scanning off, the model's self-reported casing variants are raised.
    g = Glossary(entries=[GlossaryEntry(canonical="Upper City", kind="place",
                                        variants=["Upper city"])])
    paras = [_para("body-0", "They climbed toward the Upper city gate.")]
    out = case_drift_findings(g, paras, itertools.count(1), scan=False)
    assert len(out) == 1
    assert out[0].force_query is True
    assert out[0].corrected_text == "They climbed toward the Upper City gate."


def test_case_drift_ignores_variants_that_differ_by_more_than_case():
    # "Annie" is a different name from "Anastasia", not a casing slip.
    g = Glossary(entries=[GlossaryEntry(canonical="Anastasia", kind="character",
                                        variants=["Annie"])])
    assert case_drift_findings(g, [_para("body-0", "Annie waved.")],
                               itertools.count(1)) == []


def test_case_drift_leaves_ordinary_single_words_alone():
    # A canonical with no capital past the first char carries no casing signal.
    g = Glossary(entries=[GlossaryEntry(canonical="Sword", kind="thing",
                                        variants=["sword"])])
    assert case_drift_findings(g, [_para("body-0", "He drew his sword.")],
                               itertools.count(1)) == []


def test_case_drift_can_be_absent_when_no_variants():
    g = Glossary(entries=[GlossaryEntry(canonical="The Squall", kind="thing",
                                        variants=[])])
    assert case_drift_findings(g, [_para("body-0", "The Squall grew.")],
                               itertools.count(1)) == []


# --- cache --------------------------------------------------------------------

def test_glossary_cache_writes_then_reuses_without_a_second_call(tmp_path):
    prov = FakeProvider([ProviderResult(parsed={
        "entries": [{"canonical": "Upper City", "kind": "place", "variants": []}],
        "suspected_misspellings": []})])
    paras = [_para("body-0", "The Upper City gleamed above the plain.")]
    g1 = build_glossary(paras, prov, model="m", max_tokens=100, usage=Usage(),
                        cache_dir=str(tmp_path))
    assert g1.entries[0].canonical == "Upper City"
    assert len(prov.results) == 0                     # the one scripted result was used
    # A second build with NO scripted results must still succeed — served from
    # cache, no API call. (FakeProvider with an empty queue returns an empty
    # findings payload, which would NOT parse as a Glossary, so a cache miss
    # here would surface as an empty glossary, not this one.)
    prov2 = FakeProvider([])
    g2 = build_glossary(paras, prov2, model="m", max_tokens=100, usage=Usage(),
                        cache_dir=str(tmp_path))
    assert [e.canonical for e in g2.entries] == ["Upper City"]
    assert prov2.calls == []                           # never called the provider


def test_glossary_cache_key_changes_with_the_model(tmp_path):
    paras = [_para("body-0", "The Upper City gleamed above the plain.")]
    prov = FakeProvider([ProviderResult(parsed={
        "entries": [{"canonical": "Upper City", "kind": "place", "variants": []}],
        "suspected_misspellings": []})])
    build_glossary(paras, prov, model="model-a", max_tokens=100, usage=Usage(),
                   cache_dir=str(tmp_path))
    # A different model must miss the cache and read again (here: a fresh result).
    prov2 = FakeProvider([ProviderResult(parsed={
        "entries": [{"canonical": "Lower City", "kind": "place", "variants": []}],
        "suspected_misspellings": []})])
    g = build_glossary(paras, prov2, model="model-b", max_tokens=100, usage=Usage(),
                       cache_dir=str(tmp_path))
    assert [e.canonical for e in g.entries] == ["Lower City"]
    assert prov2.calls != []                           # the different model re-read


def test_the_cache_key_changes_with_reasoning_effort(tmp_path):
    """Effort is on the wire — it is what the provider is asked to spend — so a
    raised effort has to miss the cache. Keyed without it, an operator who lifts
    glossary.effort to chase a weak read would be served the old, cheaper one
    and see no change at all."""
    paras = [_para("body-0", "Wren crossed the Upper City before dawn.")]
    payload = {"entries": [{"canonical": "Upper City", "kind": "place",
                            "variants": []}],
               "suspected_misspellings": []}
    first = FakeProvider([ProviderResult(parsed=payload)])
    build_glossary(paras, first, model="m", max_tokens=100, usage=Usage(),
                   effort="medium", cache_dir=str(tmp_path))
    raised = FakeProvider([ProviderResult(parsed=payload)])
    build_glossary(paras, raised, model="m", max_tokens=100, usage=Usage(),
                   effort="high", cache_dir=str(tmp_path))
    assert len(raised.calls) == 1, "a raised effort was served the cheaper read"

    same = FakeProvider([])                             # empty queue
    build_glossary(paras, same, model="m", max_tokens=100, usage=Usage(),
                   effort="high", cache_dir=str(tmp_path))
    assert same.calls == []                             # unchanged effort still hits
