"""One term written more than one way.

The detection is mechanical — strip hyphens and spaces and two spellings of a
term collapse to the same key — which is also why this asks instead of
correcting. The same test cannot tell an inconsistency from a distinction, and
*awhile* and *a while* mean different things. So the negatives here matter more
than the positives: a query channel fails by crying wolf until nobody reads it.
"""
from __future__ import annotations

import itertools
import json

import docx
import pytest

from docproof.config import Config, load_config
from docproof.consistency import (CONSISTENCY_KEY, find_inconsistencies,
                                  to_findings)
from docproof.models import ParagraphRef, Usage
from docproof.pipeline import finish, prepare


def _paras(*texts):
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def _keys(report):
    return {t.key: t.dominant for t in report.terms}


# --- what it finds ------------------------------------------------------------

def test_a_hyphen_that_comes_and_goes():
    r = find_inconsistencies(_paras(
        "The blood-cursed rider never returns from the northern marches.",
        "A blood-cursed name is a heavy thing for a child to carry.",
        "She had heard of the bloodcursed before, in stories told at night."))
    assert _keys(r) == {"bloodcursed": "blood-cursed"}
    assert r.terms[0].minority_forms == ("bloodcursed",)


def test_an_open_compound_against_a_closed_one():
    """The brief's own example, and the case a single-token scan would miss:
    one of the forms is two words."""
    r = find_inconsistencies(_paras(
        "He kept the archive in safe keeping for a year and a day.",
        "The safekeeping of the archive was her only remaining duty.",
        "The safekeeping cost more than the archive was ever worth."))
    assert _keys(r) == {"safekeeping": "safekeeping"}
    assert r.terms[0].minority_forms == ("safe keeping",)


def test_the_dominant_form_is_the_one_used_most():
    r = find_inconsistencies(_paras(
        "The collar bone had never set right after the fall that winter.",
        "He rubbed his collarbone, feeling where the break had knitted.",
        "The collarbone ached whenever the weather turned to rain."))
    assert r.terms[0].dominant == "collarbone"


# --- what it leaves alone -----------------------------------------------------

@pytest.mark.parametrize("texts", [
    # English distinguishes these; they are not one term spelled two ways.
    ("She waited awhile before speaking to anyone about it at all.",
     "She waited a while longer, and then a while longer still today."),
    ("It was an everyday problem, nothing anybody would remember later.",
     "He walked past the shop every day on his way to the harbour."),
    ("She could not go, and said so plainly to everyone who asked her."
     " He cannot swim.",),
])
def test_real_english_distinctions_are_not_flagged(texts):
    assert find_inconsistencies(_paras(*texts)).terms == ()


def test_a_capital_at_the_start_of_a_sentence_is_not_a_second_spelling():
    """The noisiest false positive this scan can make: every common noun that
    ever opens a sentence appears "two ways" if capitalization counts as
    spelling. It does not."""
    r = find_inconsistencies(_paras(
        "Her mother had never once spoken of the war to anyone at all.",
        "She thought of her mother and the way she folded the linen.",
        "Later her mother came in from the garden with her hands full."))
    assert r.terms == ()


def test_case_is_set_aside_but_the_spelling_underneath_is_still_read():
    """Setting case aside must not hide a real slip that happens to sit at the
    start of a sentence — and the query quotes the book's own capitalization."""
    r = find_inconsistencies(_paras(
        "Safekeeping was all she asked of him that winter, nothing more.",
        "The safekeeping of the archive was her only remaining duty.",
        "Safe keeping was a phrase he had never once used in his life."))
    assert _keys(r) == {"safekeeping": "Safekeeping"}
    assert r.terms[0].minority_forms == ("Safe keeping",)


def test_a_plural_is_not_a_possessive_written_differently():
    """Deleting the apostrophe to match “wolf's-bane” against “wolf’s bane”
    would also match “brothers” against “brother's” — two words, not one term
    written two ways."""
    assert find_inconsistencies(_paras(
        "The brothers went down to the river before the sun had risen.",
        "The brothers argued the whole way there about nothing at all.",
        "Her brother's coat was still hanging on the hook by the door.",
    )).terms == ()


def test_the_two_apostrophes_are_still_one_apostrophe():
    """Typographic and straight apostrophes are the same character as far as
    spelling goes, so this is one term written two ways."""
    r = find_inconsistencies(_paras(
        "The wolf’s-bane grew thick along the wall behind the old chapel.",
        "He gathered wolf’s-bane in the dark without a lamp to see by.",
        "The wolf's bane he carried had dried to nothing in his pocket."))
    assert r.terms[0].dominant == "wolf’s-bane"
    assert r.terms[0].minority_forms == ("wolf's bane",)


def test_a_term_written_one_way_is_not_flagged():
    r = find_inconsistencies(_paras(
        "The blood-cursed rider never returns from the northern marches.",
        "A blood-cursed name is a heavy thing for a child to carry."))
    assert r.terms == ()


def test_an_even_split_is_not_flagged_by_default():
    """With one of each there is no dominant form to recommend, and no
    evidence which is the slip. min_dominance: 1 opts into flagging these."""
    texts = ("The watch keeper stood at the rail through the whole night.",
             "The watchkeeper stood at the rail again the following night.")
    assert find_inconsistencies(_paras(*texts)).terms == ()
    assert find_inconsistencies(_paras(*texts), min_dominance=1).terms


def test_short_terms_are_left_alone():
    """The shorter the key, the more likely two forms are unrelated English
    rather than one word spelled two ways."""
    r = find_inconsistencies(_paras(
        "The set up was wrong and the setup was wrong and the setup failed."),
        min_length=20)
    assert r.terms == ()


def test_disabled_scan_does_nothing():
    r = find_inconsistencies(_paras("The blood-cursed and the bloodcursed."),
                             enabled=False)
    assert not r.ran and r.terms == ()


# --- the findings -------------------------------------------------------------

def test_findings_are_queries_that_change_nothing():
    paras = _paras(
        "The blood-cursed rider never returns from the northern marches.",
        "A blood-cursed name is a heavy thing for a child to carry.",
        "She had heard of the bloodcursed before, in stories told at night.")
    findings = to_findings(find_inconsistencies(paras), paras)
    assert len(findings) == 1
    f = findings[0]
    assert f.error_type == CONSISTENCY_KEY
    assert f.corrected_text == f.original_text     # a query edits nothing
    assert f.finding_id.startswith("c-")           # its own id series
    assert f.para_id == "body-0002"


def test_the_question_names_both_forms_and_the_dominant_one():
    paras = _paras(
        "The blood-cursed rider never returns from the northern marches.",
        "A blood-cursed name is a heavy thing for a child to carry.",
        "She had heard of the bloodcursed before, in stories told at night.")
    said = to_findings(find_inconsistencies(paras), paras)[0].explanation
    assert "bloodcursed" in said and "blood-cursed" in said
    assert "Is the difference deliberate?" in said


def test_a_finding_anchors_to_the_sentence_around_the_outlier():
    paras = _paras(
        "The blood-cursed rider never returns from the northern marches.",
        "A blood-cursed name is a heavy thing for a child to carry.",
        "She heard nothing. The bloodcursed were only ever a story here.")
    f = to_findings(find_inconsistencies(paras), paras)[0]
    assert f.original_text == "The bloodcursed were only ever a story here."


# --- through the pipeline -----------------------------------------------------

def _run(tmp_path, texts, *, max_chunks=None, **cfg_kw):
    d = docx.Document()
    for t in texts:
        d.add_paragraph(t)
    src = tmp_path / "Book.docx"
    d.save(src)
    cfg = load_config("config/default.yaml")
    cfg.error_types = ["comma_splice"]
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    prepared = prepare(cfg, src, "config/error_types", max_chunks=max_chunks)
    return prepared, finish(prepared, [], Usage(), cfg,
                            out_dir=tmp_path / "out", source_path=src)


TEXTS = ["The blood-cursed rider never returns from the northern marches.",
         "A blood-cursed name is a heavy thing for a child to carry.",
         "She had heard of the bloodcursed before, in stories told at night."]


def test_a_run_writes_the_question_into_the_manuscript(tmp_path):
    import re
    import zipfile
    _, out = _run(tmp_path, TEXTS)
    assert out.applied == 0                      # it never corrects
    body = zipfile.ZipFile(out.reviewed_path).read("word/comments.xml").decode()
    assert "writes this term more than one way" in body


def test_it_is_reported_in_both_reports(tmp_path):
    _, out = _run(tmp_path, TEXTS)
    assert "Terms written more than one way" in out.summary_md.read_text()
    payload = json.loads(out.findings_json.read_text())
    terms = payload["consistency"]["terms"]
    assert terms and terms[0]["dominant"] == "blood-cursed"


def test_a_partial_run_does_not_make_a_whole_document_claim(tmp_path):
    """"You write this three ways" is not something a review of two chapters
    is in a position to say."""
    prepared, _ = _run(tmp_path, TEXTS, max_chunks=1)
    assert prepared.consistency.terms == ()


def test_the_audit_still_passes(tmp_path):
    _, out = _run(tmp_path, TEXTS)
    assert json.loads(out.findings_json.read_text())["audit"]["passed"] is True


def test_the_shipped_config_enables_it():
    cfg = load_config("config/default.yaml")
    assert cfg.consistency.enabled and cfg.consistency.min_dominance == 2
    assert Config().consistency.enabled
