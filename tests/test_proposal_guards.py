"""The Cooper QA's bad edits, each refused, and the good edits beside them.

Fixtures are whole paragraphs shrunk by the workflow's own ``_minimal``, so a
case here is the exact ``(before, replacement, start, end)`` tuple ``_apply``
would see rather than a hand-built span.
"""
from __future__ import annotations

import pytest

from galley.fixed_workflow import _minimal
from galley.proposal_guards import (comma_adjacent_ellipsis, flat_tag_question,
                                    pronoun_number_change, proposal_problem,
                                    time_24h_conversion)


def edit(paragraph: str, corrected: str):
    """The proposal as the workflow shrinks it: (before, replacement, start, end)."""
    lo, hi, replacement = _minimal(paragraph, corrected)
    return paragraph[lo:hi], replacement, lo, hi


def problem(paragraph: str, corrected: str) -> str | None:
    before, replacement, lo, hi = edit(paragraph, corrected)
    return proposal_problem(before, replacement, paragraph, lo, hi)


# --- the Cooper cases, all refused -------------------------------------------

COOPER = [
    # (c) A time already written in 24-hour form is never converted.
    ("The log said the drone lifted at 18:03 UTC, nine minutes late.",
     "The log said the drone lifted at 6:03 p.m. UTC, nine minutes late.",
     "24-hour"),
    ("She wrote it down twice: 17:05 local. 00:05 UTC.",
     "She wrote it down twice: 5:05 p.m. local. 12:05 a.m. UTC.",
     "24-hour"),
    # (b) A plural pronoun narrowed with only a plural antecedent in view.
    ("Marcus and Dana lingered over the last of the wine. Then they stood and left.",
     "Marcus and Dana lingered over the last of the wine. Then he stood and left.",
     "plural"),
    # (d) A deliberately flat line of speech is not a question.
    ("“Did he,” she said mildly.", "“Did he?” she said mildly.", "flatly said"),
    ("“Isn’t she beautiful.” Ruth said evenly.",
     "“Isn’t she beautiful?” Ruth said evenly.", "flatly said"),
    # (e) No comma is set against an ellipsis.
    ("“Sofia … someone else saw it,” he said.",
     "“Sofia, … someone else saw it,” he said.", "ellipsis"),
]


@pytest.mark.parametrize("paragraph,corrected,reason", COOPER)
def test_the_cooper_bad_edits_are_refused(paragraph, corrected, reason):
    found = problem(paragraph, corrected)
    assert found is not None and reason in found


# --- the good edits beside them ----------------------------------------------

ALLOWED = [
    # A 12-hour time still gets its house formatting.
    ("She left at 3PM sharp.", "She left at 3:00 p.m. sharp."),
    # A leading zero on a time that already names its meridiem is 12-hour.
    ("The call came in at 08:21 PM and nobody answered.",
     "The call came in at 8:21 p.m. and nobody answered."),
    # One man in the paragraph: the reader's pronoun fix may well be right.
    ("Marcus set down his glass. Then they stood and left.",
     "Marcus set down his glass. Then he stood and left."),
    # A plain reporting tag carries no claim that the line was flat.
    ("“Is it,” she asked.", "“Is it?” she asked."),
    # An ordinary comma, nowhere near an ellipsis.
    ("After the meeting we left.", "After the meeting, we left."),
    # A comma respaced beside an existing ellipsis adds no comma.
    ("He waited,… then knocked.", "He waited, … then knocked."),
]


@pytest.mark.parametrize("paragraph,corrected", ALLOWED)
def test_good_edits_are_not_refused(paragraph, corrected):
    assert problem(paragraph, corrected) is None


# --- the guards on their own --------------------------------------------------

@pytest.mark.parametrize("before,replacement", [
    ("18:03 UTC", "6:03 p.m. UTC"),
    ("00:05 UTC", "12:05 a.m. UTC"),
    ("08:21", "8:21 a.m."),
    ("18:03", "18:03 p.m."),
    ("the shuttle left at 0830 hours", "the shuttle left at 8:30 a.m."),
    ("briefed at thirteen hundred", "briefed at 1:00 p.m."),
    ("mustered at oh-eight-thirty", "mustered at 8:30 a.m."),
])
def test_time_24h_conversion_refuses_a_conversion(before, replacement):
    assert time_24h_conversion(before, replacement) is not None


@pytest.mark.parametrize("before,replacement", [
    ("3PM", "3:00 p.m."),
    ("3:00 PM", "3:00 p.m."),
    ("08:21 PM", "8:21 p.m."),          # 12-hour, written with a leading zero
    ("18:03 UTC", "18:03 UTC,"),        # the token is left alone
    ("at 1830 in the village", "at 1830 in the hamlet"),   # a year, not a time
    ("thirteen hundred head of cattle", "thirteen hundred head of sheep"),
])
def test_time_24h_conversion_allows_everything_else(before, replacement):
    assert time_24h_conversion(before, replacement) is None


@pytest.mark.parametrize("before,replacement,refused", [
    ("Sofia … someone", "Sofia, … someone", True),
    ("Sofia … someone", "Sofia …, someone", True),
    ("Sofia ... someone", "Sofia, ... someone", True),
    ("waited,… then", "waited, … then", False),
    ("Sofia … someone", "Sofía … someone", False),
])
def test_comma_adjacent_ellipsis(before, replacement, refused):
    assert (comma_adjacent_ellipsis(before, replacement) is not None) is refused


def test_pronoun_guard_needs_a_one_sided_paragraph():
    plural = "Marcus and Dana lingered over the wine. Then "
    assert pronoun_number_change("they", "he", plural + "they stood.",
                                 len(plural), len(plural) + 4) is not None
    # Both kinds of antecedent in view: the models see the scene, not this regex.
    mixed = "Marcus and Dana argued, but he had already paid. Then "
    assert pronoun_number_change("they", "he", mixed + "they stood.",
                                 len(mixed), len(mixed) + 4) is None
    # A pronoun with nothing before it has no antecedent to contradict.
    assert pronoun_number_change("They", "He", "They stood and left.", 0, 4) is None
    # The mirror case: one man, and the edit makes him plural.
    singular = "Marcus set down his glass. Then "
    assert pronoun_number_change("he", "they", singular + "he stood.",
                                 len(singular), len(singular) + 2) is not None
    # A pronoun swap of the same number is not this guard's business.
    assert pronoun_number_change("he", "she", singular + "he stood.",
                                 len(singular), len(singular) + 2) is None


def test_flat_tag_question_refuses_only_the_flat_tag_and_the_echo():
    flat = "“Did he,” she said mildly."
    assert flat_tag_question(",", "?", flat, 7, 8) is not None
    echo = "“Someone else saw it.” “Someone else saw it,” he said."
    assert flat_tag_question(",", "?", echo, echo.rindex(",”"), echo.rindex(",”") + 1) is not None
    plain = "“Is it,” she asked."
    assert flat_tag_question(",", "?", plain, 6, 7) is None
    # Nothing to do with a question mark: an ordinary comma-to-period fix.
    assert flat_tag_question(",", ".", flat, 7, 8) is None


# --- the Immanuel possessives (2026-09-22) -----------------------------------

def test_a_possessive_converted_against_the_authors_form_is_refused():
    from docproof.consistency import possessive_policy
    policy = possessive_policy({f"p{i}": f"It was Dolores’ {thing}." for i, thing in
                                enumerate(("hand", "bag", "palm", "shoulder"))})
    paragraph = "He waits for Dolores’ approval, which she gives tacitly with her broadest grin."
    before, replacement, lo, hi = edit(paragraph, paragraph.replace("Dolores’", "Dolores’s"))
    assert (before, replacement) == ("", "s")
    assert "Dolores’" in proposal_problem(before, replacement, paragraph, lo, hi, possessives=policy)
    # Without the run's policy the guard has nothing to judge by.
    assert proposal_problem(before, replacement, paragraph, lo, hi) is None
    # Other edits in the same paragraph are untouched.
    before, replacement, lo, hi = edit(paragraph, paragraph.replace("tacitly ", ""))
    assert proposal_problem(before, replacement, paragraph, lo, hi, possessives=policy) is None
