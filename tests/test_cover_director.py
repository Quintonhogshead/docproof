"""docproof/cover/director.py: the one call that reads the book.

Every test drives assign_concepts against a fake Provider, so nothing here
touches a network. What is under test is the director's OWN contract: that
the whole manuscript reaches the call, that an over-long one is sliced rather
than truncated, that a fabricated archetype is fatal while surplus art
prompts are merely dropped, and that a wrong concept count never becomes a
job that spends image dollars.
"""
from __future__ import annotations

import pytest

from docproof.cover import director
from docproof.cover.director import (DirectorError, assign_concepts,
                                     fit_manuscript)
from docproof.cover.model import Brief
from docproof.providers import ProviderResult


class _Provider:
    """Records the one call it is given and answers with scripted JSON."""

    def __init__(self, parsed, **result_kw):
        self.parsed = parsed
        self.result_kw = result_kw
        self.seen: dict = {}

    def complete_structured(self, **kwargs):
        self.seen = kwargs
        if isinstance(self.parsed, Exception):
            raise self.parsed
        return ProviderResult(parsed=self.parsed, **self.result_kw)


def _brief(**overrides) -> Brief:
    data = dict(title="Longsword", author="Q. Johnson", genre="literary",
                pitch="A board-game understudy survives the crash that killed "
                      "his team.", concepts=1)
    data.update(overrides)
    return Brief(**data)


def _direction(archetype="full_bleed_art", prompts=None) -> dict:
    return {
        "concept_name": "The Piece", "rationale": "The book's own key image.",
        "archetype": archetype,
        "palette": {"background": "#101010", "primary": "#e8e2d6",
                    "accent": "#e0621f", "text": "#f4efe6",
                    "scrim": "#000000"},
        "title_font": "Libre Caslon Display", "author_font": "Space Mono",
        "art_prompts": prompts if prompts is not None else [
            {"slot": "background", "prompt": "An empty apron at dusk.",
             "treatment": "photo_soft", "mask_intent": ""}],
        "texture": False, "recipe": "quiet_literary", "type_move": "",
        "emphasis_word": ""}


def _answer(*directions) -> dict:
    return {"reading": "A book about absence.",
            "concepts": [{"direction": d,
                          "execution_notes": "Generate the ground.",
                          "done_when": "It reads at thumbnail size."}
                         for d in (directions or (_direction(),))]}


# -- fit_manuscript ------------------------------------------------------------

def test_a_book_inside_the_budget_is_passed_through_whole_and_unlabelled():
    text = "word " * 2000
    fitted, words, sliced = fit_manuscript(text)
    assert fitted == text
    assert words == 2000
    assert sliced is False
    assert "[THE OPENING]" not in fitted


def test_an_over_long_book_is_sliced_across_its_whole_length_not_truncated():
    """A truncated head is a worse brief than honest slices: a cover
    designed off the first 120k words of a 300k-word novel is designed off
    the setup, and the book's own answer never reaches the director."""
    words = [f"w{i}" for i in range(300_000)]
    fitted, count, sliced = fit_manuscript(" ".join(words))

    assert sliced is True
    assert count == 300_000
    assert len(fitted.split()) <= director.MAX_BOOK_WORDS
    labels = [l for l in fitted.splitlines() if l.startswith("[")]
    assert labels[0] == "[THE OPENING]"
    assert labels[-1] == "[THE ENDING]"
    assert len(labels) == director.BOOK_SLICES
    # the ending really is the ending, not another middle
    assert "w299999" in fitted


def test_the_ending_and_the_opening_get_double_weight():
    words = [f"w{i}" for i in range(300_000)]
    fitted, _, _ = fit_manuscript(" ".join(words))
    chunks = fitted.split("\n\n")
    lengths = [len(c.split()) - 1 for c in chunks]      # minus the label token
    assert lengths[0] == lengths[-1] == pytest.approx(lengths[1] * 2, rel=0.02)


# -- assign_concepts -----------------------------------------------------------

def test_the_whole_book_reaches_the_call_and_the_read_is_reported():
    provider = _Provider(_answer())
    result = assign_concepts(_brief(), provider, n=1,
                             manuscript="THE WHOLE BOOK " * 100)

    assert "THE WHOLE BOOK" in provider.seen["user"]
    assert "The complete manuscript follows" in provider.seen["user"]
    assert result.words_read == 300
    assert result.sliced is False
    assert result.reading == "A book about absence."
    assert len(result.assignments) == 1
    assert result.assignments[0].execution_notes == "Generate the ground."


def test_the_brief_and_the_shelves_reach_the_system_prompt():
    provider = _Provider(_answer())
    assign_concepts(_brief(mood="elegiac", avoid="no swords"), provider, n=1)

    system = provider.seen["system"]
    assert "full_bleed_art" in system            # the archetype shelf
    assert "Libre Caslon Display" in system      # the font shelf
    assert "quiet_literary" in system            # the recipe shelf
    assert "ART LAYERS ONLY" in system
    user = provider.seen["user"]
    assert "elegiac" in user and "no swords" in user


def test_no_manuscript_is_said_out_loud_rather_than_faked():
    provider = _Provider(_answer())
    result = assign_concepts(_brief(), provider, n=1)
    assert "No manuscript was supplied" in provider.seen["user"]
    assert result.words_read == 0


def test_a_fabricated_archetype_is_fatal():
    """Fatal because nothing downstream can render it -- and the next step
    after this call spends real image-generation dollars."""
    provider = _Provider(_answer(_direction(archetype="dragon_emblem")))
    with pytest.raises(DirectorError, match="not one of the shipped"):
        assign_concepts(_brief(), provider, n=1)


def test_a_prompt_for_a_slot_this_archetype_does_not_generate_is_dropped():
    """Not fatal: build_spec never reads it, and a live run once lost three
    paid-for concepts to one stray slot name."""
    provider = _Provider(_answer(_direction(prompts=[
        {"slot": "background", "prompt": "A real slot.", "treatment": "none",
         "mask_intent": ""},
        {"slot": "foreground", "prompt": "Not on this archetype.",
         "treatment": "none", "mask_intent": ""}])))
    result = assign_concepts(_brief(), provider, n=1)
    slots = [p.slot for p in result.assignments[0].direction.art_prompts]
    assert slots == ["background"]


def test_the_wrong_number_of_concepts_is_refused():
    provider = _Provider(_answer(_direction(), _direction()))
    with pytest.raises(DirectorError, match="but got 2"):
        assign_concepts(_brief(), provider, n=1)


def test_a_truncated_structured_reply_is_an_error_not_an_empty_job():
    provider = _Provider(None, stop_reason="max_tokens",
                         error="output truncated")
    with pytest.raises(DirectorError, match="did not return any"):
        assign_concepts(_brief(), provider, n=1)


def test_a_schema_mismatch_names_the_schema_not_the_stack():
    provider = _Provider({"reading": "x", "concepts": [{"nope": 1}]})
    with pytest.raises(DirectorError, match="did not match the schema"):
        assign_concepts(_brief(), provider, n=1)


def test_a_transport_failure_is_wrapped_never_guessed_around():
    provider = _Provider(RuntimeError("connection reset"))
    with pytest.raises(DirectorError, match="connection reset"):
        assign_concepts(_brief(), provider, n=1)


def test_an_over_long_book_reports_that_it_was_sliced():
    provider = _Provider(_answer())
    result = assign_concepts(_brief(), provider, n=1,
                             manuscript=" ".join(f"w{i}" for i in range(200_000)))
    assert result.sliced is True
    assert result.words_read == 200_000
    assert "labelled slices" in provider.seen["user"]
