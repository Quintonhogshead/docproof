"""The one shared structural heading predicate (docproof/headings.py) and that
its two consumers — chapter segmentation and the heading title-case sweep —
agree, so a long body paragraph mis-styled as a heading is body text to both."""
from __future__ import annotations

from docproof.config import SkipConfig
from docproof.continuity import chapters
from docproof.headings import HEADING_MAX_CHARS, is_structural_heading
from docproof.models import ParagraphRef
from docproof.sweeps import heading_case_findings


def _p(text, style="Heading1", location="body", i=0):
    return ParagraphRef(f"body-{i:04d}", "word/document.xml", location, text,
                        style)


_SKIP = SkipConfig()          # sweep_only defaults to ["Heading*"]
_is_head = _SKIP.is_sweep_only


def test_short_heading_styled_line_is_a_structural_heading():
    assert is_structural_heading(_p("The Shape of Things"), _is_head) is True


def test_long_body_paragraph_misstyled_as_heading_is_not():
    long = ("This is a genuinely long paragraph of body prose that someone "
            "accidentally tagged with a Heading 3 style, and it clearly runs "
            "well past any real chapter title in length.")
    assert len(long) > HEADING_MAX_CHARS
    assert is_structural_heading(_p(long, style="Heading3"), _is_head) is False


def test_heading_ending_like_a_sentence_is_not_structural():
    assert is_structural_heading(_p("This is really a sentence."),
                                 _is_head) is False
    # a trailing period wrapped in a closing quote still disqualifies
    assert is_structural_heading(_p('He said it was "done."'), _is_head) is False


def test_a_question_or_exclamation_heading_is_allowed():
    assert is_structural_heading(_p("What Now?"), _is_head) is True
    assert is_structural_heading(_p("At Last!"), _is_head) is True


def test_non_heading_style_is_never_structural():
    assert is_structural_heading(_p("Short line", style="Normal"),
                                 _is_head) is False


def test_heading_outside_the_body_is_not():
    assert is_structural_heading(_p("Running Head", location="header"),
                                 _is_head) is False


# --- the two consumers agree -------------------------------------------------

def test_chapters_do_not_split_on_a_misstyled_long_paragraph():
    long = ("A long paragraph of narrative prose mistakenly given a heading "
            "style, far exceeding the length any true chapter title would ever "
            "reach in an actual manuscript, so it must not start a chapter.")
    paras = [
        _p("Chapter One", style="Heading1", i=0),
        _p("Real body sentence that goes here.", style="Normal", i=1),
        _p(long, style="Heading3", i=2),
        _p("More body prose after the mis-styled block.", style="Normal", i=3),
    ]
    units = chapters(paras, _is_head, min_tokens=1, max_tokens=10**9)
    # One chapter (the sole real heading), NOT split again on the long block.
    assert len(units) == 1


def test_heading_sweep_does_not_title_case_a_misstyled_long_paragraph():
    long = ("here is a long lowercase body paragraph wrongly tagged as a "
            "heading three that absolutely should not be title cased into a "
            "chapter heading by the sweep because it is real running prose")
    findings, _report = heading_case_findings([_p(long, style="Heading3")],
                                              _SKIP)
    assert findings == []


def test_heading_sweep_still_title_cases_a_real_short_heading():
    findings, _report = heading_case_findings(
        [_p("the shape of things to come", style="Heading1")], _SKIP)
    assert len(findings) >= 1
