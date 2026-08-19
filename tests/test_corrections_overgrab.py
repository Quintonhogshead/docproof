"""Keeping a correction inside the change its note describes.

The failures here are the ones an audit of a real corrected book turned up:
every one of them was reported "applied exactly", because it was — `apply`
wrote the find/replace pair it was given, and `verify` then confirmed the file
matched that pair. What was wrong was the pair, or which copy of the text it
landed on, and nothing downstream of either could see it.

Each test names the line the book actually shipped with.
"""
from __future__ import annotations

from docproof.corrections.apply import apply_to_stories
from docproof.corrections.idml import parse_story
from docproof.corrections.instructions import widen_edits_to_marks
from docproof.corrections.model import (AMBIGUOUS, APPLIED, Edit, NOT_FOUND,
                                        PARA_DELETE, is_italic_style)
from docproof.corrections.overgrab import (keep_edge_marks, keep_marked_dash,
                                           repair_pair)
from docproof.corrections.parse import parse_edits


def _one_para_story(text: str, story_id: str = "s1"):
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>'
           '<CharacterStyleRange><Content>' + text
           + '</Content></CharacterStyleRange></ParagraphStyleRange></Story>')
    return parse_story(xml.encode("utf-8"), story_id)


def _paras_story(*texts: str, story_id: str = "s1"):
    """One `ParagraphStyleRange` per paragraph — the shape `Story.isolate` can
    take a paragraph out of."""
    body = "".join(
        '<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/body">'
        f"<CharacterStyleRange><Content>{t}</Content>"
        "<Br /></CharacterStyleRange></ParagraphStyleRange>" for t in texts)
    xml = '<?xml version="1.0"?><Story>' + body + "</Story>"
    return parse_story(xml.encode("utf-8"), story_id)


def _styled_story(*runs: tuple[str, str], story_id: str = "s1"):
    """A one-paragraph story from (text, applied character style) runs."""
    body = "".join(
        f'<CharacterStyleRange AppliedCharacterStyle="{style}">'
        f"<Content>{text}</Content></CharacterStyleRange>" for text, style in runs)
    xml = ('<?xml version="1.0"?><Story><ParagraphStyleRange>' + body
           + "</ParagraphStyleRange></Story>")
    return parse_story(xml.encode("utf-8"), story_id)


def _applied(story, *edits: Edit) -> list[str]:
    outs, _ = apply_to_stories([story], list(edits))
    return [o.status for o in outs]


# --- the marks a pair drops on its way past ----------------------------------

def test_a_closing_quotation_the_replacement_forgot_is_not_deleted():
    """Shipped: “It’s all right There was another sigh. The find quoted the
    closing `.”` and the replacement did not, so both were written away and the
    quotation closed nowhere."""
    assert keep_edge_marks("alright.”", "all right", "all right") == (
        "alright", "all right")


def test_an_opening_quotation_the_replacement_forgot_is_not_deleted():
    """Shipped: All right, you two. — a whole line of dialogue left unopened."""
    assert keep_edge_marks("“Alright", "All right", "All right") == (
        "Alright", "All right")


def test_both_ends_at_once():
    """Shipped: What He pulled forward — opening quote, question mark and closing
    quote all gone in one write."""
    assert keep_edge_marks("“Waht?”", "What", 'Should this be "What"?') == (
        "Waht", "What")


def test_a_double_quote_is_dropped_where_the_note_is_about_an_apostrophe():
    """Shipped: ’Cause he is, motherfucker.” — the note asks for the apostrophe
    to turn round, and the pair took the speech mark in front of it as well."""
    assert keep_edge_marks("“‘Cause", "’Cause",
                           "Replace with drop apostrophe: ’Cause") == (
        "‘Cause", "’Cause")
    # The same shape where the apostrophe is being *added*, not turned round.
    assert keep_edge_marks("“Bout", "’Bout", "Add drop apostrophe: ’Bout") == (
        "Bout", "’Bout")


def test_a_punctuation_substitution_is_left_alone():
    """The mark the pair ends on is written over by one of its own kind — that is
    the edit, not collateral."""
    assert keep_edge_marks(" don’t know,", " don’t know.",
                           "Replace comma with period") == (
        " don’t know,", " don’t know.")
    assert keep_edge_marks("e was right,", "e was right—",
                           "Replace with em dash") == (
        "e was right,", "e was right—")


def test_a_mark_the_note_names_is_left_alone():
    assert keep_edge_marks("‘straight.’", "straight.",
                           "Remove single quote") == ("‘straight.’", "straight.")


def test_a_pure_deletion_keeps_the_marks_it_names():
    assert keep_edge_marks("‘Oh, here it comes.’", "", "Remove") == (
        "‘Oh, here it comes.’", "")


def test_a_repair_never_turns_an_edit_into_a_no_op():
    assert keep_edge_marks("hers.", "hers", "") == ("hers.", "hers")


# --- one mark, one hyphen -----------------------------------------------------

def test_an_en_dash_marked_on_a_score_stays_off_the_compound():
    """Shipped: a first-time left–footed shot ... 2–0. The mark was on the score;
    the replacement en-dashed the closed compound in front of it too."""
    assert keep_marked_dash(
        "left-footed shot into the far post. 2-0.",
        "left–footed shot into the far post. 2–0.",
        "Replace hyphen with en dash (–)") == (
        "left-footed shot into the far post. 2-0.",
        "left-footed shot into the far post. 2–0.")


def test_a_single_dash_between_letters_is_the_reviewer_s_and_stays():
    """"high school–aged" is an open compound and the en dash is right. One swap
    is the one the mark was on, whatever it sits between."""
    pair = ("high school-aged", "high school–aged")
    assert keep_marked_dash(*pair, "Replace hyphen with en dash") == pair


def test_a_pair_that_changes_words_too_is_not_a_dash_pair():
    pair = ("tap-in to go up 1-0", "tap–in to go up 2–0")
    assert keep_marked_dash(*pair, "Replace hyphen with en dash") == pair


# --- the repairs reach every list ---------------------------------------------

def test_parse_repairs_the_pair_it_is_given():
    """Both a typed list and an extracted one come through `parse_edits`, which is
    why the repair lives there and not in the model front end."""
    parsed = parse_edits([{"find": "alright.”", "replace": "all right",
                           "instruction": "all right"}])
    assert (parsed.edits[0].find, parsed.edits[0].replace) == ("alright",
                                                               "all right")


def test_a_formatting_request_is_never_repaired():
    """Its find is copied into replace unchanged; there is no pair to pull back."""
    parsed = parse_edits([{"find": "“Come Away With Me”",
                           "replace": "“Come Away With Me”",
                           "format": "roman", "instruction": "De-italicize"}])
    assert parsed.edits[0].find == "“Come Away With Me”"


def test_repair_pair_composes_both():
    assert repair_pair("“2-0 and 1-0”", "2–0 and 1–0", "en dash") == (
        "2-0 and 1-0", "2–0 and 1–0")


# --- a word is a word ---------------------------------------------------------

def test_a_one_word_find_does_not_land_inside_a_longer_word():
    """Shipped: And n, as if there was — "delete the" (marked on "the
    Winn-Dixie") found the "the" inside "then"."""
    s = _one_para_story("And then, as if there was a beacon, the Winn-Dixie lot.")
    assert _applied(s, Edit(id="e1", find="the", replace="",
                            context="as if there was a beacon, the Winn-Dixie")
                    ) == [APPLIED]
    assert s.text == "And then, as if there was a beacon, Winn-Dixie lot."


def test_a_one_word_find_with_nowhere_to_land_is_flagged():
    """Shipped: At least thwas time — "is" → "was" found the "is" inside "this".
    With nowhere whole to go it is a flag, which is a person's five seconds."""
    s = _one_para_story("At least this time, it was one of the residents.")
    assert _applied(s, Edit(id="e1", find="is", replace="was")) == [NOT_FOUND]
    assert s.text == "At least this time, it was one of the residents."


def test_an_anchor_of_several_words_may_begin_mid_word():
    """A proof line starts where the line started. "ball team, h" is the tail of
    "football team, he" and is not ambiguous for a moment."""
    s = _one_para_story("He led the football team, he said, for two years.")
    assert _applied(s, Edit(id="e1", find="ball team, h",
                            replace="ball team. H")) == [APPLIED]
    assert s.text == "He led the football team. He said, for two years."


def test_a_deleted_word_takes_one_of_its_spaces_with_it():
    s = _one_para_story("the Winn-Dixie parking lot")
    _applied(s, Edit(id="e1", find="the", replace=""))
    assert s.text == "Winn-Dixie parking lot"


# --- what the book already says -----------------------------------------------

def test_a_replacement_does_not_say_twice_what_the_book_says_once():
    """Shipped: the plaque for the Player of the Tournament tournament."""
    s = _one_para_story("the plaque for the player of the tournament. I wasn’t")
    assert _applied(s, Edit(id="e1", find="player of the",
                            replace="Player of the Tournament")) == [APPLIED]
    assert s.text == "the plaque for the Player of the Tournament. I wasn’t"


def test_a_replacement_does_not_insert_a_mark_in_front_of_the_same_mark():
    """Shipped: I walked into the Shanklins’,, trailing Isabella."""
    s = _one_para_story("I walked into the Shanklins, trailing Isabella")
    assert _applied(s, Edit(id="e1", find="Shanklins",
                            replace="Shanklins’,")) == [APPLIED]
    assert s.text == "I walked into the Shanklins’, trailing Isabella"


def test_words_the_book_does_not_carry_are_still_added():
    s = _one_para_story("the plaque for the player of the year.")
    _applied(s, Edit(id="e1", find="player of the",
                     replace="Player of the Tournament"))
    assert s.text == "the plaque for the Player of the Tournament year."


# --- a paragraph emptied is a paragraph removed --------------------------------

def test_emptying_a_paragraph_removes_it_rather_than_leaving_a_blank_line():
    """Shipped: the italic thought went and its paragraph stayed, which sets as a
    blank line between two lines of dialogue."""
    s = _paras_story("This fall has been rough.", "‘Oh, here it comes.’",
                     "What do you mean?")
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="‘Oh, here it comes.’", replace="",
             instruction="Remove")])
    assert [o.status for o in outs] == [APPLIED, APPLIED]
    assert outs[1].edit.paragraph == PARA_DELETE
    assert outs[1].edit.id == "e1-para"     # named for the correction that did it
    assert [p.text for p in s.paragraphs] == ["This fall has been rough.",
                                              "What do you mean?"]


def test_a_paragraph_the_edits_left_text_in_is_kept():
    s = _paras_story("Keep me.", "Half of this goes.")
    apply_to_stories([s], [Edit(id="e1", find="Half of this ", replace="")])
    assert [p.text for p in s.paragraphs] == ["Keep me.", "goes."]


# --- italics the way the book writes them --------------------------------------

ITALIC = "CharacterStyle/Italic"
PLAIN = "CharacterStyle/$ID/[No character style]"


def _styles(story) -> list[tuple[str, str]]:
    out = []
    for csr in story.root.iter("CharacterStyleRange"):
        for content in csr:
            if content.tag == "Content":
                out.append((content.text or "",
                            csr.get("AppliedCharacterStyle", "")))
    return out


def test_roman_clears_the_character_style_the_italics_are_actually_in():
    """Shipped: reported "applied exactly" and visibly unchanged. The book sets
    its italics in a character style, and "roman" was clearing a local FontStyle
    override that was never there."""
    s = _styled_story(("Queen’s “", PLAIN), ("We Will Rock You", ITALIC),
                      (",” bass-bass-", PLAIN))
    assert _applied(s, Edit(id="e1", find="We Will Rock You",
                            replace="We Will Rock You", format="roman")
                    ) == [APPLIED]
    assert not any(is_italic_style(style) for _text, style in _styles(s))


def test_roman_takes_the_quotation_marks_around_a_title_with_it():
    """A song title's quotes are set in whatever the title is; leaving them italic
    is the difference between the correction being made and looking made."""
    s = _styled_story(("nook. ", PLAIN), ("“Come Away with Me”", ITALIC),
                      (" began playing", PLAIN))
    _applied(s, Edit(id="e1", find="Come Away with Me",
                     replace="Come Away with Me", format="roman"))
    assert ("“Come Away with Me”", PLAIN) in _styles(s)


def test_italic_reaches_for_the_style_the_book_already_uses():
    """Rather than laying a local override beside it, which looks identical and
    will not follow the style if the designer edits it."""
    s = _styled_story(("the album ", PLAIN), ("Come Away with Me", PLAIN),
                      (" played. ", PLAIN), ("elsewhere", ITALIC))
    _applied(s, Edit(id="e1", find="Come Away with Me",
                     replace="Come Away with Me", format="italic"))
    assert ("Come Away with Me", ITALIC) in _styles(s)


def test_italic_stays_a_local_override_in_a_book_that_has_no_italic_style():
    s = _styled_story(("the album ", PLAIN), ("Come Away with Me", PLAIN))
    _applied(s, Edit(id="e1", find="Come Away with Me",
                     replace="Come Away with Me", format="italic"))
    styles = {csr.get("FontStyle") for csr in s.root.iter("CharacterStyleRange")}
    assert "Italic" in styles


# --- the line a mark was written against ---------------------------------------

def test_the_second_mark_on_a_line_still_finds_the_line_it_quoted():
    """Every mark on a proof was written against the same page. The first one
    applied changes the line the second one quoted, and before this that second
    mark lost its only anchor — the "was" → "were" beside it took away the line
    that said which "the" was meant."""
    s = _one_para_story(
        "It was time to be out. And then, as if there was a beacon, "
        "the Winn-Dixie lot was full.")
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="was", replace="were",
             context="as if there was a beacon"),
        Edit(id="e2", find="the", replace="",
             context="as if there was a beacon, the Winn-Dixie lot")])
    assert [o.status for o in outs] == [APPLIED, APPLIED]
    assert s.text == ("It was time to be out. And then, as if there were a "
                      "beacon, Winn-Dixie lot was full.")


def test_a_context_holding_several_copies_no_longer_takes_the_first():
    """Taking the first copy inside the marked run was a guess wearing an
    anchor's clothes. With nothing to choose between them it is a flag."""
    s = _one_para_story("It was here and it was there.")
    outs, _ = apply_to_stories([s], [
        Edit(id="e1", find="was", replace="were",
             context="It was here and it was there.")])
    assert outs[0].status == AMBIGUOUS
    assert s.text == "It was here and it was there."


# --- what the reviewer highlighted ---------------------------------------------

class _Mark:
    def __init__(self, **kw):
        self.__dict__.update({"id": "", "page": 1, "kind": "highlight",
                              "instruction": "", "anchor": "", "offset": -1,
                              "context": "", **kw})


def test_a_find_that_quoted_less_than_the_highlight_is_put_back():
    """Shipped: he is hoped I would move on. The reviewer highlighted "is hoping"
    and wrote "hoped"; the model matched the parts of speech and emitted
    "hoping" → "hoped", which applied perfectly and left the "is" behind."""
    edits = [Edit(id="c1", find="hoping", replace="hoped", instruction="hoped",
                  source="p1-1", page=1)]
    marks = [_Mark(id="p1-1", anchor="is hoping", instruction="hoped")]
    book = {1: "his comfort zone, he is hoping I will move on"}
    assert widen_edits_to_marks(edits, marks, book_pages=book)[0].find == (
        "is hoping")


def test_a_highlight_over_a_whole_line_never_swallows_the_line():
    """"three" written against a highlight of "…and a 3-mile" is a note on the
    numeral, not an instruction to replace the sentence with one word."""
    edits = [Edit(id="c1", find="3", replace="three", instruction="three",
                  source="p1-1", page=1, occurrence=1)]
    marks = [_Mark(id="p1-1", instruction="three",
                   anchor="“You mean ninety minutes of sprints and a 3-mile")]
    book = {1: "“You mean ninety minutes of sprints and a 3-mile run?”"}
    assert widen_edits_to_marks(edits, marks, book_pages=book)[0].find == "3"


def test_a_note_that_is_an_instruction_is_not_a_replacement():
    edits = [Edit(id="c1", find="Design", replace="design",
                  instruction="Lowercase", source="p1-1", page=1)]
    marks = [_Mark(id="p1-1", anchor="Interior Design",
                   instruction="Lowercase")]
    book = {1: "Interior Design by Manish Kumar"}
    assert widen_edits_to_marks(edits, marks, book_pages=book)[0].find == "Design"


def test_a_widened_find_the_book_does_not_carry_is_not_adopted():
    """The anchor is the PDF's rendering of the highlight, so it can carry a line
    break the book has no idea about. A widening that would not land keeps the
    narrower find that was landing."""
    edits = [Edit(id="c1", find="hoping", replace="hoped", instruction="hoped",
                  source="p1-1", page=1)]
    marks = [_Mark(id="p1-1", anchor="is hop- ing", instruction="hoped")]
    book = {1: "he is hoping I will move on"}
    assert widen_edits_to_marks(edits, marks, book_pages=book)[0].find == "hoping"
