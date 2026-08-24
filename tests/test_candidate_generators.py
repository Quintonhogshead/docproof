"""Positive and negative coverage for every deterministic candidate generator.

Each generator gets a case that should produce an error/edit candidate and a
clean case that should not (P1-05). A shared invariant test confirms no
generator's splice creates adjacent duplicate punctuation.
"""
import pytest

from docproof.candidate_generators import (
    _currency_candidates, _dialogue_candidates, _direct_address_candidates,
    _heading_candidates, _introductory_candidates, _list_candidates,
    _number_candidates, _quote_candidates, _repeated_word_candidates,
    _word_echo_candidates, generate_initial_candidates)
from docproof.config import Config
from docproof.models import DocumentModel, ParagraphRef, index_paragraphs


def _p(text, style="Normal", pid="body-0000"):
    return ParagraphRef(pid, "word/document.xml", "body", text, style, True)


def _errors(candidates):
    return [c for c in candidates
            if c.evidence["local_screening"]["decision"] == "error"]


def _decisions(candidates):
    return {c.evidence["local_screening"]["decision"] for c in candidates}


# --- dialogue_tag_punctuation ------------------------------------------------

def test_dialogue_tag_missing_punctuation_is_an_error():
    errs = _errors(_dialogue_candidates(_p('"I am here" she said.')))
    assert errs and errs[0].candidate_correction == ","


def test_dialogue_tag_with_comma_is_not_an_error():
    assert not _errors(_dialogue_candidates(_p('"I am here," she said.')))


def test_action_beat_keeps_its_period():
    # "continued his search" is narration, not a speech tag — the period stays.
    cands = _dialogue_candidates(
        _p('“When he does.” Tannithan continued his search.'))
    assert cands and not _errors(cands)
    assert _decisions(cands) == {"pass"}


def test_dual_use_verb_without_object_is_judged_not_auto_corrected():
    cands = _dialogue_candidates(
        _p('“Wait.” he continued, scanning the room.'))
    assert cands and _decisions(cands) == {"needs_model_judgment"}
    assert not _errors(cands)


def test_core_speech_verb_period_is_still_an_error():
    assert _errors(_dialogue_candidates(_p('“Stop.” she said.')))


# --- quote_balance -----------------------------------------------------------

def test_unbalanced_quotes_are_flagged_for_judgment():
    cands = _quote_candidates(_p('"I am here she said.'))
    assert cands and _decisions(cands) == {"needs_model_judgment"}


def test_balanced_quotes_pass():
    cands = _quote_candidates(_p('"I am here," she said.'))
    assert cands and _decisions(cands) == {"pass"}


# --- introductory_comma (see also test_candidate_double_comma) ---------------

def test_strong_intro_missing_comma_is_an_error():
    errs = _errors(_introductory_candidates(_p("However he left.")))
    assert errs and errs[0].candidate_correction == ","


def test_strong_intro_with_comma_passes():
    assert not _errors(_introductory_candidates(_p("However, he left.")))


@pytest.mark.parametrize("text", [
    "No matter how many times I asked.",
    "No one dons a Masque and escapes.",
    "No longer will they wait.",
    "Instead of forcing it, I waited.",
    "First base was empty.",
])
def test_determiner_and_phrase_openers_are_not_comma_errors(text):
    # The Johnson run applied "No, matter", "No, servant girl", "Instead, of" as
    # hard errors. These openers must never auto-insert; the clear phrase traps
    # generate nothing at all.
    cands = _introductory_candidates(_p(text))
    assert not _errors(cands)


def test_interjection_opener_is_judged_not_auto_inserted():
    # "No servant girl" — determiner, but not in the excluded phrase list — must
    # go to the judge with the sentence, never straight to an edit.
    cands = _introductory_candidates(_p("No servant girl is worth the city."))
    assert cands and _decisions(cands) == {"needs_model_judgment"}
    assert not _errors(cands)


# --- direct_address_comma ----------------------------------------------------

def test_direct_address_missing_comma_is_an_error():
    errs = _errors(_direct_address_candidates(_p("Hello John how are you?")))
    assert errs and errs[0].candidate_correction == ","


def test_direct_address_with_comma_passes():
    assert not _errors(_direct_address_candidates(_p("Hello, John how are you?")))


def test_direct_address_lowercase_is_not_a_name():
    # Regression guard: "please wait" must not become a punctuation error.
    assert not _direct_address_candidates(_p("Please wait here."))


# --- number_style ------------------------------------------------------------

def test_small_numeral_gets_a_spelled_out_candidate():
    cands = _number_candidates(_p("I saw 3 dogs."))
    assert cands and cands[0].candidate_correction == "three"


def test_year_like_numeral_passes():
    cands = _number_candidates(_p("It was 1999 back then."))
    assert cands and _decisions(cands) == {"pass"}


# --- currency_style ----------------------------------------------------------

def test_currency_amount_is_a_candidate():
    cands = _currency_candidates(_p("It cost $5 exactly."))
    assert cands and _decisions(cands) == {"needs_model_judgment"}


def test_currency_absent_yields_nothing():
    assert not _currency_candidates(_p("It cost five dollars."))


# --- repeated_word -----------------------------------------------------------

def test_accidental_doubled_word_is_an_error():
    errs = _errors(_repeated_word_candidates(_p("This is is wrong.")))
    assert errs and errs[0].candidate_correction == "is"


def test_no_doubled_word_yields_nothing():
    assert not _repeated_word_candidates(_p("This is right."))


# --- word_echo ---------------------------------------------------------------

def test_nearby_echo_is_flagged_as_a_query():
    cands = _word_echo_candidates(
        _p("The soldier watched the soldier march away today."))
    assert cands and all(c.channel_preference == "query" for c in cands)


def test_no_echo_yields_nothing():
    assert not _word_echo_candidates(_p("The soldier marched away today."))


# --- heading_sequence --------------------------------------------------------

def test_heading_number_jump_is_an_error():
    paras = [_p("Chapter 1", "Heading 1", "h-0"),
             _p("Chapter 3", "Heading 1", "h-1")]
    errs = _errors(_heading_candidates(paras))
    assert errs and errs[0].candidate_correction == "Chapter 2"


def test_heading_sequence_in_order_passes():
    paras = [_p("Chapter 1", "Heading 1", "h-0"),
             _p("Chapter 2", "Heading 1", "h-1")]
    assert not _errors(_heading_candidates(paras))


# --- list_punctuation --------------------------------------------------------

def test_inconsistent_list_endings_need_judgment():
    paras = [_p("- first item.", "List Bullet", "l-0"),
             _p("- second item", "List Bullet", "l-1")]
    cands = _list_candidates(paras)
    assert cands and "needs_model_judgment" in _decisions(cands)


def test_consistent_list_endings_pass():
    paras = [_p("- first item.", "List Bullet", "l-0"),
             _p("- second item.", "List Bullet", "l-1")]
    cands = _list_candidates(paras)
    assert cands and _decisions(cands) == {"pass"}


# --- homophone / confusable signals ------------------------------------------

def test_confusable_fires_only_on_misuse_signals():
    from docproof.candidate_generators import _homophone_candidates

    # Misuse patterns fire...
    for text, word in [
        ("They left there bags on the platform.", "there"),
        ("Your going to be late again.", "Your"),
        ("The dog wagged it's tail happily.", "it's"),
        ("She is taller then him by a foot.", "then"),
        ("It was to late for apologies.", "to"),
        ("He walked passed the old chapel.", "passed"),
        ("It came form the cellar below.", "form"),
    ]:
        cands = _homophone_candidates(_p(text))
        assert any(c.observed_text == word for c in cands), text
        # A signal is a question for the judge, never a local edit.
        assert all(c.candidate_correction is None for c in cands)

    # ...and correct usage generates nothing at all (the Johnson canary judged
    # 5,214 unsignaled homophones for ~zero errors).
    for text in [
        "They went there today, but their bags stayed.",
        "You're going to make it in time.",
        "The dog wagged its tail happily.",
        "She is taller than him by a foot.",
        "It was too late for apologies.",
        "He walked past the old chapel.",
        "It came from the cellar below.",
    ]:
        assert _homophone_candidates(_p(text)) == [], text


# --- compound_sentence_comma --------------------------------------------------

def test_compound_join_missing_comma_is_flagged_for_judgment():
    from docproof.candidate_generators import _compound_comma_candidates

    text = "The rain hammered the windows all night and she watched it fall."
    cands = _compound_comma_candidates(_p(text))
    flagged = [c for c in cands
               if c.evidence["local_screening"]["decision"]
               == "needs_model_judgment"]
    assert flagged and flagged[0].candidate_correction == ","
    a = flagged[0].anchors[0]
    spliced = text[:a.start_offset] + "," + text[a.end_offset:]
    assert "night, and she watched" in spliced


def test_compound_join_with_comma_passes():
    from docproof.candidate_generators import _compound_comma_candidates

    text = "The rain hammered the windows all night, and she watched it fall."
    cands = _compound_comma_candidates(_p(text))
    assert cands and all(
        c.evidence["local_screening"]["decision"] == "pass" for c in cands)


def test_compound_join_ignores_lists_and_short_fragments():
    from docproof.candidate_generators import _compound_comma_candidates

    # "bread and butter" — no pronoun subject after the conjunction.
    assert _compound_comma_candidates(
        _p("She bought the bread and butter at the market stall.")) == []
    # Short first fragment — not a clause worth setting off.
    assert _compound_comma_candidates(_p("He waved and she smiled.")) == []


# --- cross-generator invariant ----------------------------------------------

def test_no_generator_ever_proposes_a_splice_with_duplicate_punctuation():
    texts = [
        '"Stop" he said.', '"Stop," he said.', "However he left.",
        "Hello John.", "I saw 3 dogs.", "It cost $5.", "This is is wrong.",
        "The soldier watched the soldier march away today.",
        "After the war, the men returned.", "- a.", "- b",
    ]
    paras = tuple(_p(t, pid=f"body-{i:04d}") for i, t in enumerate(texts))
    doc = DocumentModel("book.docx", paras)
    by_id = index_paragraphs(doc)
    types = Config().candidate_screening.candidate_types
    for c in generate_initial_candidates(doc, paras, candidate_types=types):
        a = c.anchors[0]
        if a.paragraph_id is None or a.start_offset is None:
            continue
        if c.candidate_correction is None or c.channel_preference == "query":
            continue
        text = by_id[a.paragraph_id].text
        spliced = text[:a.start_offset] + c.candidate_correction + text[a.end_offset:]
        for pair in (",,", ";;", "::"):
            assert pair not in spliced or pair in text, (
                f"{c.candidate_type} produced {pair!r}: {spliced!r}")
