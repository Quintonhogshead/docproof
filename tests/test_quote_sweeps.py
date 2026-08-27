"""The three quote-shaped catches the human pass made and the model glided
over (DP-002/DP-005): punctuation left outside a closing quote, a quotation
that never closes, and double quotes nested inside dialogue.
"""
from __future__ import annotations

import pytest

from docproof.models import ParagraphRef
from docproof.sweeps import (SWEEPS_BY_KEY, apply_hits,
                             unclosed_quote_findings)
from docproof.variants import load_variant


def swept(key: str, text: str, variant=None) -> str:
    return apply_hits(text, SWEEPS_BY_KEY[key].scan(text, variant))


def unchanged(key: str, text: str, variant=None) -> bool:
    return not SWEEPS_BY_KEY[key].scan(text, variant)


# --- the dash gap: a spaced single hyphen ------------------------------------

@pytest.mark.parametrize("before,after", [
    ("It was late - too late to matter.", "It was late—too late to matter."),
    ("He waited - and waited.", "He waited—and waited."),
])
def test_spaced_single_hyphen_becomes_an_em_dash(before, after):
    assert swept("sweep_dash", before) == after


@pytest.mark.parametrize("text", [
    "a well-known man with a half-read book",   # compounds
    "pages 5 - 7 cover the flight",             # arithmetic / loose range
    "- a list line someone typed",              # a lead-in bullet
    "the co- and post-war years",               # suspended hyphen
])
def test_single_hyphens_that_are_not_dashes_are_left_alone(text):
    assert unchanged("sweep_dash", text)


# --- punctuation against a closing double quote ------------------------------

@pytest.mark.parametrize("before,after", [
    ('He said “thanks”.', 'He said “thanks.”'),
    ('“Carol is calling”, she said.', '“Carol is calling,” she said.'),
    ('It stood “for the time being”.', 'It stood “for the time being.”'),
])
def test_period_and_comma_move_inside_the_closing_quote(before, after):
    assert swept("sweep_quote_punctuation", before) == after


def test_a_quotation_already_terminated_drops_the_outer_mark():
    assert swept("sweep_quote_punctuation", '“Go!”.') == '“Go!”'


@pytest.mark.parametrize("before,after", [
    ('so “rosy”.” I had', 'so “rosy.” I had'),          # doubled close: collapse
    ('“forced into isolation”.” She', '“forced into isolation.” She'),
    ('“Go!”.” He left', '“Go!” He left'),               # terminated + doubled close
])
def test_a_doubled_closing_quote_is_collapsed_not_stranded(before, after):
    # A malformed `”.”` (common in author manuscripts) must not become `.””` —
    # the sweep consumes the trailing duplicate closing quote and emits one.
    assert swept("sweep_quote_punctuation", before) == after


@pytest.mark.parametrize("text", [
    'a straight "quote". left alone',        # normalizer could not place it
    'the pipe was 5”. It leaked.',           # an inch mark
    '“Hello,”. she tried',                   # malformed both ways: judgment
])
def test_quote_punctuation_stays_off_ambiguous_marks(text):
    assert unchanged("sweep_quote_punctuation", text)


def test_quote_punctuation_respects_single_primary_variants():
    uk = load_variant("uk")
    assert unchanged("sweep_quote_punctuation", 'He said “thanks”.', uk)


# --- nested quotations --------------------------------------------------------

def test_nested_doubles_demote_to_singles():
    text = '“They called it the “Wave of Autonomy” on air,” he said.'
    assert swept("sweep_nested_quote", text) == \
        '“They called it the ‘Wave of Autonomy’ on air,” he said.'


@pytest.mark.parametrize("text", [
    '“No nesting here,” she said.',
    'Narration with “one quote” only.',
    '“Unbalanced “inner quote,” he said.',        # counts off: leave whole para
    'A straight " spoils “the count”.',           # normalizer punted; so do we
])
def test_nested_quote_sweep_stays_off_uncertain_paragraphs(text):
    assert unchanged("sweep_nested_quote", text)


def test_nested_quote_sweep_is_idempotent():
    text = '“the “I commit to the Ultimatum” button,” he read.'
    once = swept("sweep_nested_quote", text)
    assert swept("sweep_nested_quote", once) == once


# --- unclosed quotations -------------------------------------------------------

def paras(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def test_a_quotation_that_never_closes_is_queried():
    ps = paras('“You never see it coming, he says without blinking. '
               'Steve says, agitated.',
               'The room emptied slowly.')
    (f,) = unclosed_quote_findings(ps)
    assert f.para_id == "body-0000"
    assert f.force_query and f.corrected_text == f.original_text
    assert "closing quote may be missing" in f.explanation


def test_speech_continuing_into_the_next_paragraph_is_the_convention():
    ps = paras('“It went on and on, longer than anyone thought.',
               '“And then it stopped,” he said.')
    assert unclosed_quote_findings(ps) == []


def test_a_stray_closing_quote_is_queried():
    ps = paras('The last word was his,” and no one argued.',
               'Nothing else happened.')
    (f,) = unclosed_quote_findings(ps)
    assert "no opening partner" in f.explanation


def test_balanced_and_straight_quote_paragraphs_are_left_alone():
    ps = paras('“All balanced,” she said.',
               'He kept a straight " right here forever.')
    assert unclosed_quote_findings(ps) == []


def test_unclosed_check_respects_single_primary_variants():
    uk = load_variant("uk")
    ps = paras('“Never closed at all.', 'Plain narration.')
    assert unclosed_quote_findings(ps, uk) == []


# --- punctuation against a closing SINGLE quote (Violyam, DP-Johnson) ---------

@pytest.mark.parametrize("before,after", [
    ("It meant ‘older sibling was orphaned alongside me’.",
     "It meant ‘older sibling was orphaned alongside me.’"),
    ("He called it ‘the long way’, and smiled.",
     "He called it ‘the long way,’ and smiled."),
    # Already-terminal quotations drop the mark outside, like doubles do.
    ("She whispered ‘go!’.", "She whispered ‘go!’"),
])
def test_single_close_punctuation_moves_inside(before, after):
    assert swept("sweep_quote_punctuation", before) == after


@pytest.mark.parametrize("text", [
    "That was the boys’.",                  # possessive: no ‘ ever opened
    "It was the Joneses’, all of it.",      # same, mid-sentence
    "He is 6’.",                            # a feet mark after a digit
    "Don’t.",                               # intra-word apostrophe
])
def test_single_close_punctuation_leaves_apostrophes(text):
    assert unchanged("sweep_quote_punctuation", text)


def test_single_close_left_alone_for_uk():
    uk = load_variant("uk")
    assert unchanged("sweep_quote_punctuation",
                     "It meant ‘orphaned alongside me’.", uk)
