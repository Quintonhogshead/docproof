"""The dictionary scan.

The single most important assertion in this file is that a coined word is
never handed to the model as something to correct. The dictionary will happily
suggest "Lilith" for "Kaelith"; the whole design exists so that suggestion
never reaches an edit.
"""
from __future__ import annotations

import pytest

from docproof.analyzer import build_system_prompt
from docproof.error_registry import load_error_types
from docproof.models import ParagraphRef
from docproof.spellscan import SpellScan, scan

from .test_error_types import ERROR_DIR

spylls = pytest.importorskip("spylls", reason="the spell scan needs spylls")


def paras(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


# No suggestions by default: they cost about a quarter-second each and most
# tests here are about classification, which needs none.
def run(*texts: str, **kw) -> SpellScan:
    kw.setdefault("suggestion_limit", 0)
    return scan(paras(*texts), **kw)


def test_a_repeated_unknown_word_is_the_authors():
    result = run("Kaelith crossed the marches before dawn.",
                 "By evening Kaelith had not returned.")
    # Reported the way the author writes it, not as the lowercase key it was
    # counted under.
    assert result.lexicon == ("Kaelith",)
    assert not result.candidates


def test_a_capitalized_unknown_word_is_a_name_even_used_once():
    """Mid-sentence capitalization is the signal. A name used once is still a
    name, and correcting it would rename a character."""
    result = run("The road to Vorrenth was long and badly kept.")
    assert [w.lower() for w in result.lexicon] == ["vorrenth"]
    assert not result.candidates


def test_a_singleton_lowercase_unknown_is_a_candidate():
    result = run("She did not recieve the summons until the third day.")
    assert [c.word for c in result.candidates] == ["recieve"]
    assert not result.lexicon


def test_a_capitalized_word_at_a_sentence_start_is_not_taken_for_a_name():
    """Otherwise every typo that happens to open a sentence would be filed as
    the author's own and protected from correction."""
    result = run("The door was shut. Teh handle would not turn.")
    assert [c.word for c in result.candidates] == ["Teh"]
    assert not result.lexicon


def test_known_words_are_not_reported():
    result = run("The quick brown fox jumped over the lazy dog.")
    assert not result.lexicon and not result.candidates
    assert result.tokens == 9


def test_the_allowlist_wins_over_the_dictionary():
    result = run("The Atmosphere imprint published it.",
                 "Atmosphere published it again.",
                 allowlist=["Atmosphere"])
    assert not result.lexicon and not result.candidates


def test_min_occurrences_moves_the_line():
    texts = ("the wibble broke", "the wibble broke again")
    assert [w.lower() for w in run(*texts).lexicon] == ["wibble"]
    # Demand three sightings and twice is no longer enough to be protected.
    strict = run(*texts, min_occurrences=3)
    assert not strict.lexicon and [c.word for c in strict.candidates] == ["wibble"]


def test_suggestions_are_offered_only_for_candidates():
    """Never for the lexicon: asking what "Kaelith" should have been is
    exactly the mistake this module exists to prevent."""
    result = scan(paras("Kaelith rode on. Kaelith did not look back.",
                        "She did not recieve the summons."),
                  suggestion_limit=10)
    assert [w.lower() for w in result.lexicon] == ["kaelith"]
    candidate = next(c for c in result.candidates if c.word == "recieve")
    assert "receive" in candidate.suggestions


def test_the_suggestion_limit_is_respected():
    result = scan(paras("recieve seperate occured definately alot."),
                  suggestion_limit=2)
    enriched = [c for c in result.candidates if c.suggestions]
    assert len(enriched) == 2


def test_disabled_scan_returns_nothing_and_says_so():
    result = run("Kaelith rode on.", enabled=False)
    assert not result.available
    assert not result.lexicon and not result.candidates
    assert result.prompt_section() == ""


def test_a_missing_dictionary_degrades_instead_of_failing():
    result = run("Kaelith rode on.", dictionary="not_a_real_dictionary")
    assert not result.available
    assert result.prompt_section() == ""


# --- what the model is actually told ------------------------------------------

def test_the_prompt_section_protects_the_lexicon_and_only_queries_candidates():
    result = scan(paras("Kaelith crossed the Vorrenth marches.",
                        "Kaelith and the Vorrenth had an old quarrel.",
                        "She did not recieve the summons."),
                  suggestion_limit=0)
    section = result.prompt_section()
    assert "Kaelith" in section and "Vorrenth" in section
    assert "never change one into the standard English word" in section
    # The candidate is offered for a look, not for a correction.
    assert "recieve" in section
    assert "If it reads as deliberate, leave it." in section


def test_an_empty_scan_adds_nothing_to_the_prompt():
    """A clean manuscript should not pay tokens for an empty section."""
    assert run("The quick brown fox jumped.").prompt_section() == ""


def test_the_vocabulary_reaches_the_system_prompt():
    types = list(load_error_types(ERROR_DIR, ["spelling"]).values())
    # A word the shipped prompts do not already mention, so the assertion is
    # about the wiring rather than about spelling.yaml's own examples.
    vocab = run("Thrennigar waited. Thrennigar always waited.").prompt_section()
    prompt = build_system_prompt(types, vocabulary=vocab)
    assert "Thrennigar" in prompt
    # ...and a run without a scan is byte-for-byte what it always was.
    assert "Thrennigar" not in build_system_prompt(types)
