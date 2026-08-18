"""Corrections on a book whose paragraphs are lines.

A proof of poetry breaks the assumption every other part of this engine rests on:
that a reviewer's sentence and the book's paragraph are the same run of text. In
verse each line is its own paragraph, so a quoted sentence routinely straddles two
or three of them — and the engine's answer to a span that crossed a break was to
refuse it, which is right for a word swap and wrong for the two things a reviewer
most often asks about a line of verse: join these, and split this.

Three mechanisms are covered together because one book needed all three:

  * a context anchor matched **across** breaks, which only ever narrows and so is
    safe to widen — this is what tells two copies of "siren's" apart when the
    sentence naming the right one spans two lines;
  * `merge-next`, deleting the break between two paragraphs;
  * `split-at`, making one where there was none.

And the fourth thing on the same proof, which is not about breaks at all: a swash
capital is an OpenType feature on a character range, so "swoop the R" is as
appliable as "italicize this" and was being handed to a designer.
"""
from __future__ import annotations

from docproof.corrections.apply import apply_to_stories
from docproof.corrections.idml import parse_story
from docproof.corrections.instructions import resolve
from docproof.corrections.model import (AMBIGUOUS, APPLIED, Edit, PARA_MERGE_NEXT,
                                        PARA_SPLIT_AT, UNPLACEABLE)
from docproof.corrections.parse import parse_edits

VERSE = "ParagraphStyle/Verse"


def _story(*paragraphs: str, story_id: str = "s1", style: str = VERSE):
    """A story of one paragraph per argument, each its own range."""
    body = "".join(
        f'<ParagraphStyleRange AppliedParagraphStyle="{style}">'
        f'<CharacterStyleRange AppliedCharacterStyle="None"><Content>{p}'
        f"</Content><Br /></CharacterStyleRange></ParagraphStyleRange>"
        for p in paragraphs)
    return parse_story(('<?xml version="1.0"?><Story>' + body
                        + "</Story>").encode("utf-8"), story_id)


def _shared(*paragraphs: str, story_id: str = "s1", style: str = VERSE):
    """The harder shape: every paragraph inside ONE character range, separated by
    `Br` — what InDesign actually writes for a run of verse lines set alike."""
    inner = "<Br />".join(f"<Content>{p}</Content>" for p in paragraphs)
    xml = (f'<?xml version="1.0"?><Story><ParagraphStyleRange '
           f'AppliedParagraphStyle="{style}"><CharacterStyleRange '
           f'AppliedCharacterStyle="None">{inner}</CharacterStyleRange>'
           f"</ParagraphStyleRange></Story>")
    return parse_story(xml.encode("utf-8"), story_id)


def texts(story):
    """The story's paragraphs after a serialize/reparse round trip — read back off
    the XML, so a merge that only updated the in-memory list would not pass."""
    return [p.text for p in parse_story(story.serialize(), story.story_id).paragraphs]


# --- a context anchor that spans a break --------------------------------------

def test_a_context_spanning_two_lines_picks_the_right_copy():
    """The reviewer's sentence is two lines of verse; the word it names appears
    elsewhere in the poem too. Before, the context matched nothing and the edit
    came back ambiguous for a human."""
    story = _story("A siren’s call across the harbor.",
                   "And resist the pull",
                   "of a siren’s locomotive.")
    edit = Edit("e1", "siren’s", "Siren’s",
                context="And resist the pull of a siren’s locomotive.")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["A siren’s call across the harbor.",
                            "And resist the pull",
                            "of a Siren’s locomotive."]


def test_a_context_inside_one_paragraph_still_wins_over_the_wider_view():
    """The tighter answer is tried first. A context that resolves within a single
    paragraph must not be re-resolved across breaks, where a longer run of the book
    could contain a second copy."""
    story = _story("The harbor at dawn.", "the harbor at dusk.")
    edit = Edit("e1", "harbor", "harbour", context="The harbor at dawn.")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["The harbour at dawn.", "the harbor at dusk."]


def test_a_context_that_spans_breaks_and_still_matches_twice_is_flagged():
    """Widening the context is not loosening it. Two identical couplets leave the
    edit exactly as ambiguous as it always was — the direction this engine fails
    in is unchanged."""
    story = _story("And resist the pull", "of a siren’s locomotive.",
                   "And resist the pull", "of a siren’s locomotive.")
    edit = Edit("e1", "siren’s", "Siren’s",
                context="And resist the pull of a siren’s locomotive.")
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == AMBIGUOUS
    assert texts(story) == ["And resist the pull", "of a siren’s locomotive.",
                            "And resist the pull", "of a siren’s locomotive."]


# --- merging two paragraphs ---------------------------------------------------

def test_an_anchor_spanning_the_break_joins_the_two_paragraphs():
    """The reviewer quotes both sides of the break, which is how they say which
    one they mean. A space is written where the break was: paragraph text carries
    no trailing one, so the words would otherwise run together."""
    story = _story("Well, I’m dead, so I stick to dead people stuff:",
                   "Rotting, dirt, worms, the occasional haunt.",
                   "And nothing else.")
    edit = Edit("e1", "dead people stuff: Rotting", "dead people stuff: Rotting",
                paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == [
        "Well, I’m dead, so I stick to dead people stuff: Rotting, dirt, worms, "
        "the occasional haunt.",
        "And nothing else."]


def test_a_merge_leaves_every_other_break_where_it_was():
    """The hard shape: four paragraphs in one character range. Removing the second
    break must not disturb the first or the third."""
    story = _shared("First line.", "I would rather study",
                    "spelunking than answer.", "Fourth line.")
    edit = Edit("e1", "study spelunking", "study spelunking",
                paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["First line.",
                            "I would rather study spelunking than answer.",
                            "Fourth line."]


def test_a_merge_anchored_wholly_inside_one_paragraph_joins_it_to_the_next():
    """An anchor that does not reach across the break still names a paragraph, and
    "merge with the next" is unambiguous from there."""
    story = _story("The morgue is uncomplicated —", "I clean up the tags.")
    edit = Edit("e1", "uncomplicated —", "uncomplicated —",
                paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["The morgue is uncomplicated — I clean up the tags."]


def test_two_paragraphs_in_different_styles_are_refused_not_merged():
    """The survivor can only have one style, and imposing the first's on the second
    is not what a note about a line break asked for."""
    story = parse_story(
        '<?xml version="1.0"?><Story>'
        '<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/Head">'
        '<CharacterStyleRange><Content>A Heading</Content><Br /></'
        "CharacterStyleRange></ParagraphStyleRange>"
        '<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/Body">'
        "<CharacterStyleRange><Content>Body text follows.</Content><Br /></"
        "CharacterStyleRange></ParagraphStyleRange></Story>".encode("utf-8"),
        "s1")
    edit = Edit("e1", "A Heading Body text", "A Heading Body text",
                paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == UNPLACEABLE
    assert "different paragraph styles" in outcomes[0].detail
    assert texts(story) == ["A Heading", "Body text follows."]


def test_an_anchor_spanning_two_breaks_is_refused():
    """One break per edit. A quote covering three lines does not say which of the
    two breaks the reviewer wants gone."""
    story = _story("One line", "two line", "three line")
    edit = Edit("e1", "One line two line three line",
                "One line two line three line", paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == UNPLACEABLE
    assert "spans 2 paragraph breaks" in outcomes[0].detail
    assert texts(story) == ["One line", "two line", "three line"]


def test_a_merge_anchor_found_twice_is_flagged():
    story = _story("study", "spelunking", "study", "spelunking")
    edit = Edit("e1", "study spelunking", "study spelunking",
                paragraph=PARA_MERGE_NEXT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == AMBIGUOUS
    assert outcomes[0].occurrences == 2
    assert len(texts(story)) == 4


# --- splitting one paragraph --------------------------------------------------

def test_a_split_starts_a_new_paragraph_at_the_anchor():
    """The anchor is the text that should START the new paragraph, and the space
    that used to separate the two halves goes with the break."""
    story = _story("so they remain perfectly still. The next hour does not go "
                   "according to plan.", "Later.")
    edit = Edit("e1", "The next hour", "The next hour", paragraph=PARA_SPLIT_AT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["so they remain perfectly still.",
                            "The next hour does not go according to plan.",
                            "Later."]


def test_a_split_keeps_the_paragraphs_around_it():
    story = _shared("First.", "Second half one. Second half two.", "Third.")
    edit = Edit("e1", "Second half two.", "Second half two.",
                paragraph=PARA_SPLIT_AT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["First.", "Second half one.", "Second half two.",
                            "Third."]


def test_a_split_at_the_very_start_of_a_paragraph_is_refused():
    """There is nothing in front of the anchor, so the break would make an empty
    paragraph rather than divide anything."""
    story = _story("The next hour does not go to plan.", "Later.")
    edit = Edit("e1", "The next hour", "The next hour", paragraph=PARA_SPLIT_AT)
    outcomes, _ = apply_to_stories([story], [edit])
    assert outcomes[0].status == UNPLACEABLE
    assert texts(story) == ["The next hour does not go to plan.", "Later."]


# --- a swash capital ----------------------------------------------------------

def test_a_swash_is_applied_as_a_character_feature_not_routed_to_design():
    story = _story("About the Author")
    outcomes, _ = apply_to_stories([story], [Edit("e1", "Author", "Author",
                                                  format="swash")])
    assert outcomes[0].status == APPLIED
    assert texts(story) == ["About the Author"]          # not one character changed
    xml = story.serialize().decode("utf-8")
    assert 'OTFSwash="true"' in xml
    assert xml.count("Author") == 1


def test_removing_a_swash_clears_the_attribute_rather_than_overwriting_it():
    story = parse_story(
        '<?xml version="1.0"?><Story><ParagraphStyleRange>'
        '<CharacterStyleRange OTFSwash="true"><Content>Author</Content>'
        "</CharacterStyleRange></ParagraphStyleRange></Story>".encode("utf-8"),
        "s1")
    outcomes, _ = apply_to_stories([story], [Edit("e1", "Author", "Author",
                                                  format="no-swash")])
    assert outcomes[0].status == APPLIED
    assert "OTFSwash" not in story.serialize().decode("utf-8")


# --- the notes these come from ------------------------------------------------

def test_the_note_a_reviewer_writes_resolves_without_a_model():
    """Each of these was a real comment on the proof that prompted the work. The
    long anchors matter: a note about a break has to quote a whole sentence to be
    about anything, so the "a long mark points at nothing" guard cannot apply to
    them."""
    got = resolve('Please delete the line break between “stuff:” and “Rotting” '
                  "so that the paragraph breaks naturally.",
                  "I stick to dead people stuff: Rotting, dirt, worms, the "
                  "occasional haunt.")
    assert (got.paragraph, got.find) == (PARA_MERGE_NEXT, "stuff: Rotting")

    got = resolve('Please delete the line break before “dead” so that the '
                  "paragraph reads naturally.",
                  "clear out the rats, and reacquaint myself with the clarity "
                  "of the dead.")
    assert got.paragraph == PARA_MERGE_NEXT
    assert got.find.endswith("dead") and " " in got.find

    got = resolve("Please insert a paragraph break in this block of text so that "
                  "it reads “The next hour”.",
                  "they remain perfectly still. The next hour or so does not go "
                  "according to plan.")
    assert (got.paragraph, got.find) == (PARA_SPLIT_AT, "The next hour")

    got = resolve("Can you swoop the R in “Author”?", "About the Author")
    assert (got.format, got.find) == ("swash", "Author")


def test_a_question_about_a_break_is_not_carried_out():
    """Reading "should this run on?" as an instruction is the failure mode that
    matters most here: it deletes a break nobody asked to delete."""
    assert resolve("Should we delete the line break between “study” and "
                   "“spelunking”?", "I would rather study spelunking.") is None


def test_a_note_about_how_the_page_composed_is_still_a_designer_check():
    """The new rules must not swallow the notes that genuinely need InDesign."""
    got = resolve("Bad line break here — loose rag.", "some marked line of text")
    assert got.rule == "composition-check"
    assert not got.paragraph


def test_the_new_operations_survive_the_parser():
    """The ops and formats have to arrive through `parse_edits` too, which is what
    the extractor's output and a typed list both go through."""
    result = parse_edits({"edits": [
        {"find": "stuff: Rotting", "replace": "stuff: Rotting",
         "paragraph": "merge-next"},
        {"find": "The next hour", "replace": "The next hour",
         "paragraph": "split-at"},
        {"find": "Author", "replace": "Author", "format": "swash"},
    ]})
    assert not result.issues
    assert [e.paragraph for e in result.edits] == ["merge-next", "split-at", ""]
    assert result.edits[2].format == "swash"
    assert all(e.is_structural for e in result.edits[:2])


# --- how a moved break reads in the change log --------------------------------

def test_a_moved_break_is_shown_as_both_paragraphs():
    """A break op changes two paragraphs, and the report indexes it on one. Showing
    only that one reads as text having vanished — a split's "now" would be the
    first half by itself — so the side holding two is shown as both, with the break
    marked where a proofreader would mark it."""
    from docproof.corrections.verify import _review_changes

    src = ("so they remain perfectly still. The next hour does not go to plan.",
           "Later.")
    before, after = _story(*src), _story(*src)
    outcomes, _ = apply_to_stories(
        [after], [Edit("e1", "The next hour", "The next hour",
                       paragraph=PARA_SPLIT_AT)])
    change = _review_changes(outcomes, {"s1": before}, {"s1": after})[0]
    assert change.before == src[0]
    assert change.after == ("so they remain perfectly still. ¶ The next hour "
                            "does not go to plan.")

    src = ("I stick to dead people stuff:", "Rotting, dirt, worms.")
    before, after = _story(*src), _story(*src)
    outcomes, _ = apply_to_stories(
        [after], [Edit("e2", "dead people stuff: Rotting",
                       "dead people stuff: Rotting", paragraph=PARA_MERGE_NEXT)])
    change = _review_changes(outcomes, {"s1": before}, {"s1": after})[0]
    assert change.before == "I stick to dead people stuff: ¶ Rotting, dirt, worms."
    assert change.after == "I stick to dead people stuff: Rotting, dirt, worms."


def test_an_ordinary_edit_still_shows_one_line():
    """The pilcrow is for the two operations that move a break and nothing else."""
    from docproof.corrections.verify import _review_changes

    src = ("The harbor at dawn.", "Later.")
    before, after = _story(*src), _story(*src)
    outcomes, _ = apply_to_stories([after], [Edit("e1", "harbor", "harbour")])
    change = _review_changes(outcomes, {"s1": before}, {"s1": after})[0]
    assert "¶" not in change.before and "¶" not in change.after


def test_a_merge_earlier_in_the_story_does_not_misquote_a_later_change():
    """The paragraph number an edit reports is the before file's for a text edit
    and the live document's for a structural one, because the two run in different
    passes. Reading both against the same file made a split's "was" line quote
    whatever paragraph had moved into its slot — a report that says a correction
    changed a line it never touched."""
    from docproof.corrections.verify import _review_changes

    src = ("One line.", "It went on forever.", "She opened the door.",
           "A third paragraph with plain text for good measure.")
    before, after = _story(*src), _story(*src)
    outcomes, _ = apply_to_stories([after], [
        Edit("m1", "on forever. She opened", "on forever. She opened",
             paragraph=PARA_MERGE_NEXT),
        Edit("s1", "for good measure.", "for good measure.",
             paragraph=PARA_SPLIT_AT),
    ])
    assert [o.status for o in outcomes] == [APPLIED, APPLIED]
    rows = {c.edit_ids[0]: c for c in _review_changes(outcomes, {"s1": before},
                                                      {"s1": after})}
    assert rows["m1"].before == "It went on forever. ¶ She opened the door."
    assert rows["m1"].after == "It went on forever. She opened the door."
    assert rows["s1"].before == src[3]
    assert rows["s1"].after == "A third paragraph with plain text ¶ for good measure."
