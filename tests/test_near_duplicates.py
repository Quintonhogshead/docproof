"""Exemption-list hygiene: protection must not be granted to a misspelling of
a name that is already protected.

The failure this file pins (DP-001): the protected list held "Hollingworth"
one line from "Hollingsworth" — a one-letter variant of a main character's
surname — and, protected, the misspelling became the one error the pipeline
structurally could not find. 135 detections were suppressed by the list's own
instruction to keep it.
"""
from __future__ import annotations

import pytest

from docproof.adjudicate import site_word_candidates
from docproof.models import ParagraphRef
from docproof.pipeline import _name_pair_queries
from docproof.spellscan import SpellScan, scan

spylls = pytest.importorskip("spylls", reason="the spell scan needs spylls")


def paras(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def run(*texts: str, **kw) -> SpellScan:
    kw.setdefault("suggestion_limit", 0)
    return scan(paras(*texts), **kw)


def test_dominated_near_duplicate_is_demoted_not_protected():
    texts = ["Hollingsworth waited by the door for Hollingsworth business."] * 5
    texts.append("Old Hollingworth never answered.")
    result = run(*texts)
    assert "Hollingsworth" in result.lexicon
    assert "Hollingworth" not in result.lexicon
    (nd,) = result.near_duplicates
    assert (nd.word, nd.suggestion) == ("Hollingworth", "Hollingsworth")
    assert nd.of_count == 10 and nd.count == 1
    # Demoted words join the look-at pile carrying the dominant twin as their
    # suggestion, so the model reads them as suspects, not vocabulary.
    demoted = next(c for c in result.candidates if c.word == "Hollingworth")
    assert demoted.suggestions == ("Hollingsworth",)


def test_close_counts_become_a_pair_question_and_both_stay_protected():
    result = run("Hollingsworth arrived first, then Hollingsworth again.",
                 "But Hollingworth had been there all night, Hollingworth said.")
    assert {"Hollingsworth", "Hollingworth"} <= set(result.lexicon)
    assert not result.near_duplicates
    (pair,) = result.name_pairs
    assert {pair.a, pair.b} == {"Hollingsworth", "Hollingworth"}


def test_plural_cased_unlike_its_singular_is_demoted_whatever_the_ratio():
    result = run("The EVTOL hummed. Another EVTOL followed.",
                 "Two Evtols crossed overhead, and more Evtols behind them.")
    assert "EVTOL" in result.lexicon
    assert "Evtols" not in result.lexicon
    (nd,) = result.near_duplicates
    assert (nd.word, nd.suggestion, nd.of) == ("Evtols", "EVTOLs", "EVTOL")


def test_plural_cased_like_its_singular_is_left_alone():
    result = run("The EVTOL hummed and the EVTOLs answered it.",
                 "Rows of EVTOLs waited beside the lone EVTOL.")
    assert {"EVTOL", "EVTOLs"} <= set(result.lexicon)
    assert not result.near_duplicates
    assert not result.name_pairs


def test_short_names_one_letter_apart_are_two_people():
    result = run("Kai and Kaya argued. Kai lost.",
                 "Kaya laughed at Kai, and Kai at Kaya, then Kai left.")
    assert {"Kai", "Kaya"} <= set(result.lexicon)
    assert not result.near_duplicates
    assert not result.name_pairs


def test_diacritic_pairs_are_the_consistency_scans_business():
    texts = ["Rían rode out with Rían's banner and Rían's men."] * 4
    texts.append("Rian slept late.")
    result = run(*texts)
    assert not result.near_duplicates
    assert not result.name_pairs


def test_lexicon_counts_ride_alongside_the_list():
    result = run("Kaelith crossed. Kaelith slept beside Vorrenth.")
    counts = dict(zip(result.lexicon, result.lexicon_counts))
    assert counts["Kaelith"] == 2
    assert counts["Vorrenth"] == 1


def test_name_pair_queries_anchor_at_the_rarer_spelling():
    ps = paras("Hollingsworth arrived first, then Hollingsworth again.",
               "But Hollingworth had been there all night, Hollingworth said.")
    spell = scan(ps, suggestion_limit=0)
    (finding,) = _name_pair_queries(spell, ps)
    assert finding.force_query
    assert finding.corrected_text == finding.original_text
    assert finding.para_id == "body-0001"
    assert "Hollingworth" in finding.explanation
    assert "Hollingsworth" in finding.explanation


def test_site_word_candidates_sites_every_occurrence():
    ps = paras("Hollingworth stood. Later, HOLLINGWORTH shouted.",
               "Nothing about Hollingsworth here.")
    cands = site_word_candidates({"Hollingworth": "Hollingsworth"}, ps,
                                 kind="near_dup")
    assert [c.word for c in cands] == ["Hollingworth", "HOLLINGWORTH"]
    assert all(c.suggestion == "Hollingsworth" for c in cands)
    assert all(c.kind == "near_dup" for c in cands)
    # Word-bounded: the dominant name itself is never sited.
    assert all(c.para_id == "body-0000" for c in cands)
