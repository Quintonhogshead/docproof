"""The whole-book narrative tense profiler (docproof/tensecheck.py).

The invariant every test here leans on: this module reports, it never edits —
so the assertions are about the *map* (baseline, runs, verdicts), never about
corrections. The scenarios mirror the calibration case: a past-tense book
carrying whole scenes in the historical present, which the sentence-internal
tense_shift detector is structurally blind to.
"""
from __future__ import annotations

import json

from docproof.models import ParagraphRef
from docproof.tensecheck import profile, render


def _para(text, i=0, location="body", reviewable=True):
    return ParagraphRef(f"body-{i:04d}", "word/document.xml", location, text,
                        "Normal", reviewable=reviewable)


# Dense, unambiguous signal paragraphs. PAST: walked, looked, fell, pulled,
# rattled (4 regular -ed + 1 irregular). PRESENT: walks, looks, pulls,
# reaches (all on the curated list).
PAST = ("She walked to the window and looked out at the street. The rain "
        "fell in sheets, and she pulled her coat tighter as the wind "
        "rattled the glass.")
PRESENT = ("She walks to the window and looks out at the street. The rain "
           "drums on the glass, and she pulls her coat tighter as the wind "
           "reaches under the door.")


def _book(spec):
    """spec: sequence of (text,) laid out in document order with ids 0..n."""
    return [_para(text, i) for i, text in enumerate(spec)]


# --- the calibration shape: past book, one present scene -------------------

def test_past_book_with_one_present_scene_surfaces_exactly_that_run():
    texts = [PAST] * 6 + [PRESENT] * 3 + [PAST] * 4
    p = profile(_book(texts))
    assert p.baseline == "past"
    assert len(p.runs) == 1
    assert p.runs[0].para_ids == ("body-0006", "body-0007", "body-0008")
    assert p.runs[0].words > 0
    assert p.runs[0].sample.startswith("She walks to the window")
    # every paragraph classified, none dropped
    assert p.narration_paragraphs == 13
    assert 0.0 < p.present_share < 0.5


def test_non_reviewable_and_furniture_paragraphs_are_excluded():
    paras = _book([PAST] * 5)
    paras.append(_para(PRESENT, 90, reviewable=False))       # heading-like
    paras.append(_para(PRESENT, 91, location="header"))      # furniture
    p = profile(paras)
    assert p.narration_paragraphs == 5
    assert p.baseline == "past"
    assert p.runs == ()
    assert p.present_share == 0.0


# --- dialogue is stripped before counting ----------------------------------

def test_present_tense_dialogue_does_not_flip_a_past_narration_paragraph():
    text = ("“I am here, and I know what he says to them, and I think he "
            "sees it too,” she said. She turned away and walked back down "
            "the hall.")
    p = profile([_para(text, 0)] + [_para(PAST, i) for i in range(1, 5)])
    entry = p.paragraphs[0]
    assert entry.verdict == "past"
    assert entry.present == 0            # the quoted presents never counted
    assert entry.past >= 3               # said, turned, walked


def test_straight_quotes_and_unclosed_quotes_strip_too():
    # unclosed opening quote strips to end of paragraph
    text = 'She nodded and smiled and waited. "I am here and he says'
    p = profile([_para(text)])
    entry = p.paragraphs[0]
    assert entry.present == 0
    assert entry.past == 3               # nodded, smiled, waited
    assert entry.verdict == "past"


# --- uniformly present book ------------------------------------------------

def test_uniform_present_book_has_present_baseline_and_no_runs():
    p = profile(_book([PRESENT] * 6))
    assert p.baseline == "present"
    assert p.runs == ()
    assert p.present_share == 1.0
    assert all(e.verdict == "present" for e in p.paragraphs)


# --- evidence floor and run continuity -------------------------------------

def test_thin_paragraphs_get_none_and_do_not_break_a_run():
    thin = "She paused."                  # one signal (paused) < 3: none
    texts = [PAST] * 6 + [PRESENT, thin, PRESENT] + [PAST] * 4
    p = profile(_book(texts))
    assert p.baseline == "past"
    thin_entry = next(e for e in p.paragraphs if e.para_id == "body-0007")
    assert thin_entry.verdict == "none"
    # the two present paragraphs bridge across the "none" beat into ONE run
    assert len(p.runs) == 1
    assert p.runs[0].para_ids == ("body-0006", "body-0008")
    # "none" paragraphs neither vote nor count as classified
    assert p.narration_paragraphs == 12


def test_a_single_short_against_paragraph_stays_below_the_run_floor():
    texts = [PAST] * 6 + [PRESENT] + [PAST] * 6
    p = profile(_book(texts))
    assert p.baseline == "past"
    assert p.runs == ()                  # 1 paragraph, well under 120 words


# --- person detection ------------------------------------------------------

def test_first_person_narration_is_detected():
    text = ("I walked to the door and I looked back at my house. My hands "
            "shook as I pulled my coat around me, and I felt my heart "
            "against my ribs.")
    p = profile([_para(text, i) for i in range(3)])
    assert p.person == "first"


def test_third_person_narration_is_detected():
    text = ("He turned to her and she looked at him, and they walked to "
            "their car while he held her hand and they watched the road.")
    p = profile([_para(text, i) for i in range(3)])
    assert p.person == "third"


def test_too_few_pronouns_is_unclear():
    p = profile([_para("The rain rattled the glass and pooled on the sill.")])
    assert p.person == "unclear"


# --- render ----------------------------------------------------------------

def test_render_names_the_baseline_and_the_run_paragraphs():
    texts = [PAST] * 6 + [PRESENT] * 3 + [PAST] * 4
    out = render(profile(_book(texts)))
    assert "PAST" in out
    assert "body-0006" in out and "body-0008" in out
    assert "present-dominant" in out
    # the closing reminder: this is a map, not an apply path
    assert "nothing here auto-applies" in out


def test_render_warns_when_the_baseline_is_not_held():
    texts = [PAST] * 9 + [PRESENT] * 4   # 4/13 ≈ 31% against: over the 15% bar
    out = render(profile(_book(texts)))
    assert "NOT" in out


def test_render_handles_an_unclear_baseline():
    texts = [PAST, PRESENT] * 5          # 50/50 by words: no baseline
    p = profile(_book(texts))
    assert p.baseline == "unclear"
    assert p.runs == ()
    out = render(p)
    assert "UNCLEAR" in out


# --- json ------------------------------------------------------------------

def test_to_json_round_trips_through_json_dumps():
    texts = [PAST] * 6 + [PRESENT] * 3 + [PAST] * 4
    p = profile(_book(texts))
    d = p.to_json()
    restored = json.loads(json.dumps(d))
    assert restored == d
    assert restored["baseline"] == "past"
    assert restored["narration_paragraphs"] == 13
    assert restored["runs"][0]["para_ids"] == ["body-0006", "body-0007",
                                               "body-0008"]
    assert len(restored["paragraphs"]) == 13
    assert set(restored["paragraphs"][0]) == {"para_id", "verdict", "past",
                                              "present", "sample"}
