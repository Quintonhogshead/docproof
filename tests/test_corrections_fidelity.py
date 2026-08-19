"""The fidelity gate: a deterministic read of each edit against the mark it came
from, holding back the ones it can prove are unfaithful and reclassifying a question
the extractor answered as a concrete change.

The cases are drawn straight from the Shams Book 7 QA pass — the seventeen wrong
edits whose mechanical apply was perfect and whose result was not — plus the guards
that keep the gate from flagging the good edits beside them.
"""
from __future__ import annotations

from docproof.corrections.fidelity import screen_edits
from docproof.corrections.model import Edit, JUDGMENT, MECHANICAL


def _comment(cid, anchor, note, kind="highlight", offset=0):
    return {"id": cid, "anchor": anchor, "instruction": note, "kind": kind,
            "offset": offset, "page": int(cid.split("-")[0][1:])}


def _screen(edit, comment, book_pages=None):
    out, withheld = screen_edits([edit], [comment], book_pages=book_pages or {})
    return out[0], withheld


# --- withholds: the change is not faithful to the mark -------------------------

def test_an_edit_on_the_wrong_page_is_withheld():
    """The p157 mark that landed on page 181's identical wording: the text it changes
    is not on the page the mark was made on, but is elsewhere in the book."""
    e = Edit(id="c1", find="around,” she paused", replace="around.” She paused",
             page=157, source="p157-1",
             instruction="Replace comma with period and capitalize \"she\"")
    book = {157: "a different line entirely on page 157.",
            181: "when he was around,” she paused, took a breath."}
    _out, withheld = _screen(e, _comment("p157-1", "she", "Replace comma with "
                                         "period and capitalize \"she\""), book)
    assert "c1" in withheld and "not on page 157" in withheld["c1"]


def test_a_change_outside_the_highlight_is_withheld():
    """"Lowercase both" marked on "Hundred Percent" that also lowercased the "One"
    before it — a word the highlight does not cover."""
    e = Edit(id="c1", find="One Hundred Percent.", replace="one hundred percent.",
             page=78, source="p78-1", instruction="Lowercase both")
    _out, withheld = _screen(e, _comment("p78-1", "Hundred Percent.”", "Lowercase both"))
    assert "c1" in withheld and "One" in withheld["c1"]


def test_a_punctuation_change_away_from_the_mark_is_withheld():
    """"Remove comma" marked on "But, the playoffs" that removed the comma in
    "expected, another" instead — its neighbours are nowhere in the highlight."""
    e = Edit(id="c1", find="was expected, another", replace="was expected another",
             page=308, source="p308-1", instruction="Remove comma")
    anchor = ("for a deep playoff run. But, the playoffs")
    _out, withheld = _screen(e, _comment("p308-1", anchor, "Remove comma"))
    assert "c1" in withheld and "expected" in withheld["c1"]


def test_replacing_a_different_mark_than_the_note_names_is_withheld():
    """"Replace comma with colon" that turned a period into a colon instead."""
    e = Edit(id="c1", find="elsewhere. It was interesting, though,",
             replace="elsewhere: It was interesting, though,",
             page=148, source="p148-1", instruction="Replace comma with colon")
    _out, withheld = _screen(e, _comment(
        "p148-1", "whatever was made elsewhere. It was interesting, though,",
        "Replace comma with colon"))
    assert "c1" in withheld and "period" in withheld["c1"]


def test_converting_more_marks_than_asked_is_withheld():
    """"Replace with em dash" (one) that em-dashed both commas of a pair."""
    e = Edit(id="c1", find="welcomed us, a lot of people, in fact",
             replace="welcomed us—a lot of people—in fact",
             page=210, source="p210-1", instruction="Replace with em dash")
    _out, withheld = _screen(e, _comment(
        "p210-1", "Sure, some folks welcomed us, a lot of people, in fact.",
        "Replace with em dash"))
    assert "c1" in withheld and "one em dash" in withheld["c1"]


def test_a_dropped_written_out_correction_is_withheld():
    """The reviewer wrote the fix out — "she was not" — and the edit applied only
    "was not", dropping the subject."""
    e = Edit(id="c1", find="not", replace="was not", page=118, source="p118-1",
             instruction="she was not")
    _out, withheld = _screen(e, _comment("p118-1", "not", "she was not"))
    assert "c1" in withheld and "she was not" in withheld["c1"]


# --- reclassifications: a question the extractor answered -----------------------

def test_a_question_note_answered_mechanically_becomes_a_judgment():
    """"Should 'the' be removed…?" extracted as a concrete deletion is turned back
    into the judgment it should have been — a person decides, not the extractor."""
    e = Edit(id="c1", find="the", replace="", page=141, source="p141-1",
             instruction="Should \"the\" be removed as in previous instances?")
    out, withheld = _screen(e, _comment("p141-1", "the", e.instruction))
    assert out.kind == JUDGMENT and "c1" not in withheld


def test_a_recommendation_becomes_a_judgment():
    """"Recommend removing single quotes & italicizing thought" — a compound
    recommendation, half of which was applied — is a person's call."""
    e = Edit(id="c1", find="‘What the fuck was that?’", replace="What the fuck was that?",
             page=25, source="p25-1",
             instruction="Recommend removing single quotes & italicizing thought")
    out, _ = _screen(e, _comment("p25-1", "‘What the fuck was that?’", e.instruction))
    assert out.kind == JUDGMENT


# --- the guards: good edits pass through untouched ------------------------------

def test_a_quote_conversion_is_not_read_as_an_outside_mark_change():
    """Replacing a passage's single quotes with doubles rewrites its marks but none
    of its words, so it must not read as a change landing outside the highlight."""
    e = Edit(id="c1", find="‘If you aren’t going to speak to me’",
             replace="“If you aren’t going to speak to me”",
             page=1, source="p1-1", instruction="Replace single quotes with doubles")
    _out, withheld = _screen(e, _comment(
        "p1-1", "‘If you aren’t going to speak to me’",
        "Replace single quotes with doubles"))
    assert withheld == {}


def test_capital_t_is_an_instruction_not_a_dropped_literal():
    """"Capital T" means capitalize the T, not insert the words "Capital T" — the
    dropped-literal check must not fire on it."""
    e = Edit(id="c1", find="t-shirt", replace="T-shirt", page=1, source="p1-1",
             instruction="Capital T")
    _out, withheld = _screen(e, _comment("p1-1", "white t-shirt", "Capital T"))
    assert withheld == {}


def test_a_bare_would_literal_is_not_a_question():
    """"would" written beside "will" is the corrected word, not a question — it must
    apply, not be reclassified."""
    e = Edit(id="c1", find="will", replace="would", page=1, source="p1-1",
             instruction="would")
    out, withheld = _screen(e, _comment("p1-1", "will see", "would"))
    assert out.kind == MECHANICAL and withheld == {}


def test_a_faithful_edit_passes_the_gate():
    """A plain correction whose change is inside the mark, on the right page, doing
    what the note names, is untouched."""
    e = Edit(id="c1", find="mud room", replace="mudroom", page=25, source="p25-1",
             instruction="mudroom")
    book = {25: "entered the mud room, removed his shoes."}
    out, withheld = _screen(e, _comment("p25-1", "mud room", "mudroom"), book)
    assert out.kind == MECHANICAL and withheld == {}


def test_a_typed_list_with_no_comments_is_never_touched():
    """No marks to check against — every edit passes through, whatever it says."""
    e = Edit(id="c1", find="teh", replace="the")
    out, withheld = screen_edits([e], [], book_pages={})
    assert out == [e] and withheld == {}
