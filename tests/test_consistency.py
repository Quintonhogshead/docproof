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


# --- case is not a structural difference --------------------------------------
# The single largest noise source in real output: English capitalizes the first
# word of every sentence, so almost any common word appears both lowercased and
# sentence-initial. That is not a term written two ways, and must never be
# flagged — while the genuine open/closed/hyphen differences underneath it must
# survive untouched.

def test_sentence_initial_capital_is_not_an_inconsistency():
    r = find_inconsistencies(_paras(
        "You should always warm up before the heavier compound lifts begin.",
        "you should try the omega-rich foods first, she wrote in the margin.",
        "You should rest between sets, and you should hydrate throughout too.",
        "In her notes she repeated that you should never skip the cool down."))
    assert r.terms == ()


def test_all_caps_variant_is_not_an_inconsistency():
    r = find_inconsistencies(_paras(
        "The animals grazed while the other animals watched from the ridge line.",
        "ANIMALS, the sign said, in letters a foot tall above the pen gate.",
        "She counted the animals twice and still came up one animal short."))
    assert "animals" not in _keys(r)


def test_real_open_vs_closed_compound_still_flagged_despite_case_noise():
    r = find_inconsistencies(_paras(
        "They overconsume protein and then overconsume it again the next day.",
        "Do not over consume the supplement, the label warned in small print.",
        "Overconsume it and the benefit plateaus; overconsume it further and it harms."))
    assert _keys(r) == {"overconsume": "overconsume"}
    assert r.terms[0].minority_forms == ("over consume",)
    # The sentence-initial "Overconsume" shares the dominant structure and must
    # not be dragged in as an outlier.
    assert all(o.form != "Overconsume" for o in r.terms[0].outliers)


def test_hyphen_variant_with_sentence_initial_capital():
    r = find_inconsistencies(_paras(
        "The blood-cursed rider never returns from the northern marches alone.",
        "Blood-cursed and weary, he crossed the bridge before the last light.",
        "She had heard of the bloodcursed before, in stories told at night."))
    assert _keys(r) == {"bloodcursed": "blood-cursed"}
    assert r.terms[0].minority_forms == ("bloodcursed",)
    assert all(o.form != "Blood-cursed" for o in r.terms[0].outliers)


def test_an_even_split_after_aggregation_is_not_flagged():
    """Aggregating case variants must not manufacture a dominant form out of a
    genuine open/closed tie — three 'over all' against two 'overall' is still
    the author's call, not a slip to nag about."""
    r = find_inconsistencies(_paras(
        "Over all it went well, over all better than the pessimists had feared.",
        "Overall the plan held; overall the numbers came in close to target.",
        "over all, she judged, the season was neither triumph nor disaster."))
    assert "overall" not in _keys(r)


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
