"""Whose grammar is it — the character's or the author's?

The setting exists because fourteen error types answer that question by
assumption, and the assumption is right for most fiction and wrong for the rest.
The assertions that matter here are the two boundaries: that `preserve` costs
nothing, and that no setting ever reaches an accent.
"""
from __future__ import annotations

import pytest

from docproof.analyzer import build_system_prompt
from docproof.config import Config, load_config
from docproof.error_registry import load_error_types
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from docproof.validator import validate_findings
from docproof.voice import Voice, in_dialogue, load_voice

from .test_error_types import ERROR_DIR


def _para(text: str) -> DocumentModel:
    return DocumentModel("m.docx", (ParagraphRef("body-0000",
                                                 "word/document.xml", "body",
                                                 text, "Normal"),))


def _finding(text: str, corrected: str) -> Finding:
    return Finding(finding_id="f-0001", chunk_id="chunk-000",
                   para_id="body-0000", error_type="subject_verb_agreement",
                   original_text=text, occurrence=1, corrected_text=corrected,
                   explanation="Agreement.", confidence="high")


# --- the setting --------------------------------------------------------------

def test_the_default_preserves_voice_and_costs_nothing():
    """A press that never thinks about this pays no tokens for it, and gets
    the behaviour the type sections already describe."""
    v = load_voice("preserve")
    assert not v.reports_dialect and not v.queries_dialect
    assert v.prompt_section() == ""
    assert Config().voice == "preserve"
    assert load_config("config/default.yaml").voice == "preserve"


@pytest.mark.parametrize("key,queries", [("query", True), ("correct", False)])
def test_the_other_two_ask_for_it_and_differ_only_in_channel(key, queries):
    v = load_voice(key)
    assert v.reports_dialect and v.queries_dialect is queries
    assert "NONSTANDARD GRAMMAR" in v.prompt_section()


def test_an_unknown_setting_is_refused_by_name():
    with pytest.raises(ValueError, match="preserve, query, correct"):
        load_voice("ignore")


def test_the_section_withdraws_the_clause_it_contradicts():
    """Fourteen type sections say to leave dialect alone. A section that
    quietly disagreed with them would leave the model holding two instructions
    and no way to rank them, so it says which one wins."""
    said = load_voice("correct").prompt_section()
    assert "that instruction is withdrawn" in said
    assert "this section wins" in said


def test_no_setting_ever_reaches_an_accent():
    """Grammar, never pronunciation. "Nobody never found" is an error;
    "nothin'" is how a word sounds, and levelling it deletes a character."""
    for key in ("query", "correct"):
        said = load_voice(key).prompt_section()
        for spelling in ("nothin'", "gonna", "ain't", "y'all", "'bout"):
            assert spelling in said, key
        assert "deletes an accent" in said


# --- where it lands in the prompt ---------------------------------------------

def test_the_section_is_read_before_the_types_it_overrides():
    types = load_error_types(ERROR_DIR, ["subject_verb_agreement", "spelling"])
    prompt = build_system_prompt(list(types.values()),
                                 voice=load_voice("correct").prompt_section())
    assert prompt.index("NONSTANDARD GRAMMAR") < prompt.index("ERROR TYPE:")


def test_a_preserved_voice_adds_no_section_at_all():
    types = load_error_types(ERROR_DIR, ["subject_verb_agreement"])
    plain = build_system_prompt(list(types.values()))
    same = build_system_prompt(list(types.values()),
                               voice=load_voice("preserve").prompt_section())
    assert plain == same


# --- the query channel --------------------------------------------------------

SPEECH = 'He said, “The rules was clear.” The rules were not clear at all.'


def test_query_turns_a_correction_inside_speech_into_a_question():
    """Nobody's speech is edited: the anchor covers the sentence and inserts
    nothing, which is what a margin comment is."""
    doc = _para(SPEECH)
    f = _finding("The rules was clear.", "The rules were clear.")
    out = validate_findings([f], doc, "medium", dialect_queries=True)
    assert out[0].status == "query"
    assert out[0].anchor.insert_text == ""


def test_query_leaves_narration_a_correction():
    """Only the speech is in question. An error outside the quotation marks is
    the author's own and is corrected as usual."""
    doc = _para(SPEECH)
    f = _finding("The rules were not clear at all.",
                 "The rules was not clear at all.")
    out = validate_findings([f], doc, "medium", dialect_queries=True)
    assert out[0].status == "validated"


def test_without_the_setting_speech_is_corrected_like_anything_else():
    """`voice: correct` routes nothing specially — that is the whole
    difference between it and `query`."""
    doc = _para(SPEECH)
    f = _finding("The rules was clear.", "The rules were clear.")
    out = validate_findings([f], doc, "medium", dialect_queries=False)
    assert out[0].status == "validated"


# --- reading the quotation marks ----------------------------------------------

@pytest.mark.parametrize("text,needle,inside", [
    ('He said, “Nobody never found it.” She nodded.', "never", True),
    ('He said, “Nobody never found it.” She nodded quick.', "quick", False),
    ('“I don’t know,” he said, “and I never did.”', "never did", True),
    ('“I don’t know,” he said. She left.', "She left", False),
    # A speech running on into the next paragraph is written with no closing
    # mark at all, so this is counted rather than matched.
    ('“It runs on into the next paragraph', "runs", True),
])
def test_double_quoted_speech(text, needle, inside):
    assert in_dialogue(text, text.index(needle)) is inside


@pytest.mark.parametrize("text,needle,inside", [
    ('‘Nobody never found it,’ he said.', "never", True),
    # The closing mark and the apostrophe are the same character: "don’t" must
    # not close the speech it is standing inside.
    ('‘I don’t know,’ he said. She left.', "She left", False),
    ('‘I don’t know that,’ he said.', "know that", True),
])
def test_single_quoted_speech_survives_the_apostrophe(text, needle, inside):
    assert in_dialogue(text, text.index(needle), single=True) is inside


def test_a_default_voice_object_preserves():
    assert Voice().key == "preserve"
