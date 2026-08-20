"""Fewer marks left for a human: the levers that place, narrow, and confirm.

Four changes cut what a corrections run flags without ever guessing at one:

  * the page map places sparse pages (a title, a one-line poem) on a probe that
    occurs once in the book, so a mark on such a page narrows instead of hunting
    the whole book and landing on the wrong copy;
  * an edit whose `find` crosses a poem's line break but whose change sits on one
    side is narrowed to that paragraph and applied, instead of refused;
  * the last tier is told when a reviewer's note *proposes* a specific fix, so a
    "should this be 'lightning'?" the book confirms is carried out, not deferred.

The sanity gate's block-quotation softening (lever 4) is a prompt change with no
deterministic surface, so it is exercised by the suite not regressing rather than
asserted here.
"""
from __future__ import annotations

from docproof.corrections.apply import _narrow_across_break, apply_to_stories
from docproof.corrections.escalate import (_brief, escalate_queries,
                                           is_period_cap_query,
                                           proposed_replacement)
from docproof.corrections.idml import parse_story
from docproof.corrections.model import (APPLIED, ApplyReport, CROSSES_PARAGRAPH,
                                        Edit, JUDGMENT, MECHANICAL, NO_CHANGE)
from docproof.corrections.pagemap import build_page_map
from docproof.corrections.run import _apply_status_of, _reconcile_comments
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from .fakes import FakeProvider


def _story(*paras, story_id="s1"):
    body = "".join(
        '<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/Body">'
        f'<CharacterStyleRange AppliedCharacterStyle="None"><Content>{p}</Content>'
        "<Br /></CharacterStyleRange></ParagraphStyleRange>" for p in paras)
    return parse_story(('<?xml version="1.0"?><Story>' + body
                        + "</Story>").encode("utf-8"), story_id)


# --- Lever 1: sparse-page placement ------------------------------------------

def test_a_short_distinctive_page_is_placed_on_a_unique_probe():
    book = _story("DIAGNOSIS",
                  "The quick brown fox jumps over the lazy dog by the river.",
                  "A closing paragraph of ordinary prose to round the page out.")
    # Page 1 is a nine-character title — far too short to vote, but "DIAGNOSIS"
    # occurs once, so it places.
    pm = build_page_map([book], ["DIAGNOSIS",
                                 "The quick brown fox jumps over the lazy dog by "
                                 "the river."])
    assert pm.knows(1)
    assert pm.knows(2)


def test_a_short_page_whose_text_repeats_is_left_unplaced():
    book = _story("Refrain line.", "Some ordinary prose in between the two.",
                  "Refrain line.")
    # "Refrain line." occurs twice, so no single probe pins the page — it stays
    # unplaced rather than placed on a coin toss.
    pm = build_page_map([book], ["Refrain line."])
    assert not pm.knows(1)


def test_a_page_shorter_than_the_floor_is_skipped():
    book = _story("END", "A paragraph of prose long enough to be a real page here.")
    pm = build_page_map([book], ["END"])           # three chars, below MIN_PROBE
    assert not pm.knows(1)


# --- Lever 2: narrowing an edit that only looks cross-paragraph ---------------

def test_a_change_on_one_side_of_a_break_is_applied_in_its_paragraph():
    stories = [_story("How everyone left,", "Even his wife stayed home.")]
    edit = Edit(id="p1-1", find="How everyone left,\nEven his wife",
                replace="How everyone left,\neven his wife")
    outs, _ = apply_to_stories(stories, [edit])
    assert outs[0].status == APPLIED
    assert stories[0].paragraphs[0].text == "How everyone left,"
    assert stories[0].paragraphs[1].text == "even his wife stayed home."
    assert "\n" not in stories[0].paragraphs[1].text   # no break written in


def test_a_line_join_still_flags_as_cross_paragraph():
    stories = [_story("a reaper's reflection", "its own microscopic world")]
    # The em dash removes the break itself — a paragraph operation, not a narrow.
    edit = Edit(id="p2-1", find="a reaper's reflection\nits own",
                replace="a reaper's reflection—its own")
    outs, _ = apply_to_stories(stories, [edit])
    assert outs[0].status == CROSSES_PARAGRAPH
    assert stories[0].paragraphs[0].text == "a reaper's reflection"


def test_narrowing_declines_a_change_that_touches_two_paragraphs():
    # Both lines change, so there is no single paragraph to narrow to.
    edit = Edit(id="x", find="First line,\nSecond line.",
                replace="first line,\nsecond line.")
    assert _narrow_across_break(edit) is None


def test_narrowing_declines_when_the_break_count_changes():
    edit = Edit(id="x", find="one\ntwo", replace="one two")   # a join
    assert _narrow_across_break(edit) is None


# --- Lever 3: a reviewer-proposed fix the book can confirm --------------------

def test_proposed_replacement_reads_the_reviewers_target():
    assert proposed_replacement("Should this be 'lightning'?") == "lightning"
    assert proposed_replacement("Unsure: should this be 'eaves'?") == "eaves"
    assert proposed_replacement("should be immortality") == "immortality"
    assert proposed_replacement("Should this be 'TV on again'?") == "TV on again"


def test_a_bare_question_or_operation_proposes_nothing():
    assert proposed_replacement("Add period?") is None
    assert proposed_replacement("Capitalize?") is None
    assert proposed_replacement("Should we cut this paragraph?") is None
    assert proposed_replacement("should be removed") is None
    assert proposed_replacement("") is None


def test_the_brief_leads_with_the_proposal_when_there_is_one():
    edit = Edit(id="p1-1", find="lightening,", replace="",
                instruction="Should this be 'lightning'?", page=5)
    brief = _brief(edit, passage="…the lightening, split the sky…",
                   evidence=[], proposal="lightning")
    assert "PROPOSED" in brief
    assert "lightning" in brief
    # Without a proposal the line is absent, so a genuine query is unaffected.
    plain = _brief(edit, passage="…", evidence=[], proposal=None)
    assert "PROPOSED" not in plain


# --- Lever 5: deciding a period / capitalization mark ------------------------

# A short poem that drops terminal periods on every line, so a passage-based
# decision about a missing period has a clear convention to read off.
_POEM = ["An old sad stream", "God forgive me", "I am a lackluster shell",
         "Thrown into an old sad stream", "stained in this place and time"]


def _poem_and_scope():
    story = _story(*_POEM)
    return [story], build_page_map([story], _POEM)


def _cap_query(instruction, context, page=1):
    return Edit(id="c1", find=context, replace=context, kind=JUDGMENT,
                instruction=instruction, context=context, page=page,
                source="p1-1")


def test_a_period_query_reads_as_a_period_or_cap_question():
    assert is_period_cap_query("Add period after?")
    assert is_period_cap_query("Capitalize?")
    assert is_period_cap_query("Should this be lowercase?")
    assert not is_period_cap_query("Should this be 'mice'?")
    assert not is_period_cap_query("Remove comma")


def test_a_deliberate_open_line_is_confirmed_as_set_and_clears_the_flag():
    stories, scope = _poem_and_scope()
    edit = _cap_query("Add period after?", "God forgive me")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "leave",
                "note": "the poem ends four comparable lines without a period, "
                        "so this open line is deliberate"},
        usage=NormalizedUsage(input_tokens=600, output_tokens=40))])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    # A confirm-as-set counts with the resolved and clears the flag.
    assert (resolved, advised) == (1, 0)
    assert out[0].kind == MECHANICAL and out[0].find == out[0].replace
    assert "confirmed as set" in out[0].instruction
    # And it reconciles to a no-op that carries its reasoning, off the human list.
    apply_out, _ = apply_to_stories(stories, [out[0]])
    status_of = _apply_status_of(ApplyReport(outcomes=tuple(apply_out)))
    disp = _reconcile_comments(
        [{"id": "p1-1", "page": 1, "kind": "highlight",
          "instruction": "Add period after?", "anchor": "God forgive me"}],
        [out[0]], status_of)[0]
    assert disp.disposition == "no_op" and not disp.needs_human
    assert "deliberate" in disp.detail


def test_leave_is_ignored_on_a_query_that_is_not_about_a_period_or_capital():
    stories, scope = _poem_and_scope()
    edit = _cap_query("Should we cut this line?", "I am a lackluster shell")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "leave", "note": "reads fine to me"},
        usage=NormalizedUsage(input_tokens=500, output_tokens=30))])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    # "leave" is only for period/cap marks; here it falls through to advice and the
    # query stays a person's.
    assert (resolved, advised) == (0, 1)
    assert out[0].kind == JUDGMENT and out[0].find == out[0].replace


def test_a_missing_period_the_convention_demands_is_resolved():
    stories, scope = _poem_and_scope()
    edit = _cap_query("Add period?", "stained in this place and time")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "resolve",
                "find": "stained in this place and time",
                "replace": "stained in this place and time.",
                "context": "",
                "note": "the surrounding lines close their sentences"},
        usage=NormalizedUsage(input_tokens=600, output_tokens=40))])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    assert (resolved, advised) == (1, 0)
    assert out[0].kind == MECHANICAL
    assert out[0].replace.endswith(".")
