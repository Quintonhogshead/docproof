"""A line the book quotes back to itself, and gets wrong.

The three shapes are taken from the Cooper QA. The negatives matter at least as
much: repetition is a device, and a scan that flags a refrain teaches the press
to stop reading the channel it flags into.
"""
from __future__ import annotations

from docproof.callbacks import (CALLBACK_KEY, Callback, callback_findings,
                                find_callbacks)
from docproof.config import load_config
from docproof.consistency import find_inconsistencies, to_findings
from docproof.models import ParagraphRef

SPOKEN = "“Every war needs its swords. Decide if you're willing to be one.”"
REMEMBERED = ("The words replayed as she walked: Every war needs its swords. "
              "Decide if you are willing to be one.")


def _paras(*texts, style="Normal"):
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t, style)
            for i, t in enumerate(texts)]


def _gap(n, word="dust"):
    return [f"Outside the window the {word} rose and fell for a long while, "
            f"and the {'hour' if i % 2 else 'minute'} {i} went by unremarked."
            for i in range(n)]


def _report(paras):
    """The scan as the shipped config runs it. Off in the find_inconsistencies
    signature by design: a misquote is a tracked edit, and the caller that asks
    for tracked edits is the caller that screens them."""
    return find_inconsistencies(paras, callbacks=True)


def _edits(findings):
    return [f for f in findings if f.corrected_text != f.original_text]


def _queries(findings):
    return [f for f in findings if f.corrected_text == f.original_text]


# --- the three Cooper shapes --------------------------------------------------

def test_a_remembered_line_is_set_back_to_the_words_as_spoken():
    """Cooper's own: the spoken line has the contraction, the remembered one
    eleven paragraphs later does not. A remembered line is a quotation."""
    paras = _paras(SPOKEN, *_gap(11), REMEMBERED)
    [cb] = find_callbacks(paras)
    assert cb.kind == "misquote"
    assert cb.earlier_para_id == "body-0000" and cb.para_id == "body-0012"
    assert 0.80 <= cb.ratio < 1.0
    [f] = callback_findings([cb], paras)
    assert f.error_type == CALLBACK_KEY and f.para_id == "body-0012"
    # The span is the remembered SENTENCE, not the paragraph around it.
    assert f.original_text == "Decide if you are willing to be one."
    assert f.corrected_text == "Decide if you're willing to be one."
    assert "body-0000" in f.explanation and "88%" in f.explanation


def test_a_requoted_salutation_that_changed_is_corrected_too():
    """The second Cooper shape: an email quoted back with "Ms." become "Dr."
    A title's full stop must not end the sentence, or the one thing that
    differs lands in a fragment nothing compares."""
    letter = ("Ms. Chávez—I am writing to ask whether the observatory will "
              "release the plates from the second run.")
    paras = _paras("She read the letter twice, and then a third time.",
                   letter,
                   *_gap(4, "wind"),
                   "She read it again. " + letter.replace("Ms.", "Dr."))
    [cb] = find_callbacks(paras)
    assert cb.kind == "misquote"
    [f] = callback_findings([cb], paras)
    assert f.corrected_text.startswith("Ms. Chávez—I am writing")
    assert f.original_text.startswith("Dr. Chávez—I am writing")


def test_a_sentence_repeated_verbatim_in_one_scene_is_asked_about():
    line = ("“The array has been dark for eleven hours and nobody can tell me "
            "why,” she said into the phone.")
    paras = _paras("Chapter Four",
                   line,
                   "“It is the same story every time,” he answered, and she "
                   "heard him shuffle a stack of papers.",
                   line)
    [cb] = find_callbacks(paras)
    assert cb.kind == "verbatim" and cb.ratio == 1.0
    [f] = callback_findings([cb], paras)
    assert f.corrected_text == f.original_text          # a question, never a cut
    assert "repeated verbatim" in f.explanation
    assert "body-0001" in f.explanation


def test_a_near_match_with_no_memory_frame_is_a_question():
    paras = _paras("She had counted the crates in the hold twice before dawn, "
                   "and the number never changed.",
                   *_gap(6),
                   "She had counted the crates in the hold twice before noon, "
                   "and the number never changed.")
    [cb] = find_callbacks(paras)
    assert cb.kind == "near"
    [f] = callback_findings([cb], paras)
    assert f.corrected_text == f.original_text
    assert "Is the echo deliberate?" in f.explanation


# --- what it must leave alone -------------------------------------------------

def test_a_refrain_is_a_device_and_is_never_flagged():
    """Three or more of anything is deliberate by construction."""
    refrain = "And the sea gave back what the sea had taken, every time."
    paras = _paras(*[t for i in range(4)
                     for t in (refrain, f"The town slept on regardless, year {i}.")])
    assert find_callbacks(paras) == ()


def test_a_sentence_pattern_the_book_repeats_is_not_a_misquote():
    """The other kind of deliberate repetition: one template written out again
    and again with a detail changed. No sentence is identical, so the refrain
    rule cannot see it; a sentence in more than one near-match can."""
    paras = _paras(*[f"The corridor lights flickered as she walked, and the "
                     f"number {i} door stayed shut behind her." for i in range(5)])
    assert find_callbacks(paras) == ()


def test_short_sentences_are_not_callbacks():
    paras = _paras("He nodded once.", *_gap(3), "He nodded once.",
                   "She said nothing.", "She said nothing.")
    assert find_callbacks(paras) == ()
    # Nor is a long-enough sentence that simply does not recur.
    assert find_callbacks(_paras(SPOKEN, *_gap(4))) == ()


def test_headings_and_shouted_lines_carry_no_callbacks():
    line = ("The array has been dark for eleven hours and nobody can tell me "
            "why it went out.")
    heads = _paras(line, line, style="Heading1")
    assert find_callbacks(heads) == ()
    shouted = _paras(line.upper(), line.upper())
    assert find_callbacks(shouted) == ()


def test_a_verbatim_repeat_far_from_its_twin_is_left_to_the_author():
    """Two identical sentences four chapters apart are an echo, not a scene
    written twice — the shape the question is about is a repeat inside one
    conversation."""
    line = ("“The array has been dark for eleven hours and nobody can tell me "
            "why,” she said into the phone.")
    paras = _paras("Chapter One", line, *_gap(3),
                   "Chapter Two", *_gap(3), line)
    assert find_callbacks(paras) == ()


# --- the wiring ---------------------------------------------------------------

def test_the_scan_rides_the_consistency_report_and_its_config_switch():
    paras = _paras(SPOKEN, *_gap(11), REMEMBERED)
    report = _report(paras)
    assert [cb.kind for cb in report.callbacks] == ["misquote"]
    assert report.corrected == 1 and report.flagged == 0
    findings = [f for f in to_findings(report, paras)
                if f.error_type == CALLBACK_KEY]
    assert len(_edits(findings)) == 1
    assert find_inconsistencies(paras, callbacks=False).callbacks == ()
    assert find_inconsistencies(paras).callbacks == ()      # off unless asked
    # A sentence shorter than the floor cannot be indexed, so raising the floor
    # past the remembered line silences it.
    assert find_inconsistencies(paras, callbacks=True,
                                callback_min_tokens=40).callbacks == ()
    cfg = load_config("config/default.yaml").consistency
    assert cfg.callbacks and cfg.callback_min_tokens == 8


def test_a_query_callback_counts_as_flagged_not_corrected():
    paras = _paras("She had counted the crates in the hold twice before dawn, "
                   "and the number never changed.",
                   *_gap(6),
                   "She had counted the crates in the hold twice before noon, "
                   "and the number never changed.")
    report = _report(paras)
    assert report.flagged == 1 and report.corrected == 0
    assert len(_queries([f for f in to_findings(report, paras)
                         if f.error_type == CALLBACK_KEY])) == 1


def test_the_finding_anchors_where_the_validator_will_look():
    """`original_text` must be the sentence window ``sentence_window`` would
    quote, and the occurrence index must match it, or the edit cannot be
    placed."""
    from docproof.sweeps import sentence_window
    paras = _paras(SPOKEN, *_gap(11), REMEMBERED)
    [f] = callback_findings(find_callbacks(paras), paras)
    para = next(p for p in paras if p.para_id == f.para_id)
    assert f.original_text in para.text
    start = para.text.index(f.original_text)
    window, _, occurrence = sentence_window(
        para.text, start, start + len(f.original_text))
    assert (window, occurrence) == (f.original_text, f.occurrence)


def test_the_dataclass_keeps_both_sides_of_the_comparison():
    paras = _paras(SPOKEN, *_gap(11), REMEMBERED)
    [cb] = find_callbacks(paras)
    assert isinstance(cb, Callback)
    assert cb.earlier_text.startswith("Decide if you're willing")
    assert cb.text == "Decide if you are willing to be one."
