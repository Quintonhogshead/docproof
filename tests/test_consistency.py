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
import unicodedata

import docx
import pytest

from docproof.config import Config, load_config
from docproof.consistency import (CONSISTENCY_KEY, NAME_KEY,
                                  find_inconsistencies, find_name_drift,
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


# --- a possessive is grammar, an internal apostrophe is spelling ---------------
# A plural against a possessive — mothers / mother's / mothers' — is two
# different words that legitimately coexist, and the difference is form-final.
# It must never flag. An apostrophe *inside* a fixed term (Krebs' Cycle) is one
# term written two ways and must still flag. Word count is not the test: the
# two-word windows 'the mothers' and "the mother's" also differ only
# form-finally and must not flag either.

def test_a_plural_and_a_possessive_are_not_an_inconsistency():
    r = find_inconsistencies(_paras(
        "The mother's patience wore thin as the long afternoon dragged on.",
        "Every mother's instinct told her the storm would come by nightfall.",
        "A mother's love, the old song went, outlasts the winter and the war.",
        "The mothers gathered at the gate to wait for the returning boats."))
    assert "mothers" not in _keys(r)


def test_a_shared_preceding_word_does_not_resurrect_the_false_positive():
    """'the mothers' and "the mother's" share a key through the two-word window,
    but their only difference is still form-final — so still no inconsistency."""
    r = find_inconsistencies(_paras(
        "In the mother's house the lamps were kept burning right through dusk.",
        "The mother's house stood apart from the village, past the old mill.",
        "They said the mother's house had a room no key would ever open again.",
        "The mothers gathered there each spring to mend the fishing nets."))
    assert "themothers" not in _keys(r)


def test_an_internal_apostrophe_in_a_fixed_term_still_flags():
    r = find_inconsistencies(_paras(
        "The Krebs' Cycle governs how the cell releases the energy it stores.",
        "She sketched the Krebs' Cycle on the board in quick sure strokes.",
        "Every exam asks about the Krebs' Cycle in one form or another.",
        "The Krebs Cycle, he insisted, was the only chemistry worth knowing."))
    assert _keys(r) == {"krebscycle": "Krebs' Cycle"}
    assert r.terms[0].minority_forms == ("Krebs Cycle",)
    # The redundant preceding-word group ("The Krebs'" vs "The Krebs") differs
    # only form-finally, so folding form-final apostrophes suppresses it — an
    # intended side effect, not a regression.
    assert "thekrebs" not in _keys(r)


def test_a_lowercase_internal_possessive_still_flags():
    r = find_inconsistencies(_paras(
        "They sell late tomatoes at the farmer's market every Saturday.",
        "The farmer's market moved indoors once the autumn rains set in.",
        "She found the best pears at the farmer's market near the harbour.",
        "The farmers market on the square was the oldest one in the county."))
    assert _keys(r) == {"farmersmarket": "farmer's market"}
    assert r.terms[0].minority_forms == ("farmers market",)


def test_a_closing_quote_does_not_create_a_variant():
    """_WORD swallows the closing quote of single-quoted dialogue (blood-cursed’),
    so a word that appears bare three times and once inside quotes would flag
    against itself — the trim gives the quote back before grouping."""
    r = find_inconsistencies(_paras(
        "The rider was blood-cursed, or so the villagers always whispered.",
        "Everyone agreed the man was blood-cursed from the day of his birth.",
        "The priest would only say the child was blood-cursed and looked away.",
        "‘I always knew he was blood-cursed’ was the only thing she would say."))
    assert r.terms == ()


# --- accented terms are terms too ---------------------------------------------
# The tokenizer reads Unicode letters (and decomposed combining marks) whole,
# so fiancée is one token, not fianc + e — an accented compound is as visible
# to the scan as an ASCII one. What the scan must never do is fold the accents
# away: café against cafe may be a loanword against its anglicization, and
# accent drift is the name scan's question, asked only of proper names.

def test_an_accented_compound_hyphen_that_comes_and_goes():
    r = find_inconsistencies(_paras(
        "The café-goers lingered over their cups long past the lunch hour.",
        "Most café-goers knew better than to ask about the corner table.",
        "The cafégoers had claimed the terrace by the time she arrived."))
    assert _keys(r) == {"cafégoers": "café-goers"}
    assert r.terms[0].minority_forms == ("cafégoers",)


def test_an_accented_open_compound_against_a_closed_one():
    r = find_inconsistencies(_paras(
        "They took the après ski slowly, boots steaming against the stove.",
        "The après-ski was livelier than the slopes had been all day.",
        "Nobody dressed for the après-ski until the light had fully gone."))
    assert _keys(r) == {"aprèsski": "après-ski"}
    assert r.terms[0].minority_forms == ("après ski",)


def test_an_accent_only_difference_is_not_a_term_inconsistency():
    """fiancée and fiancee never collapse to one key. A loanword and its
    anglicized spelling can be two deliberate choices, so the term scan does
    not accent-fold — accent drift is only ever the name scan's question, and
    only for proper names."""
    r = find_inconsistencies(_paras(
        "Her fiancée wrote from the coast every week without fail.",
        "The fiancée kept every letter in the drawer beside the bed.",
        "The fiancee of the harbour master was less faithful a writer."))
    assert r.terms == ()


def test_a_decomposed_accent_is_the_same_term_as_a_composed_one():
    """One manuscript pasted together from two tools can carry café in both
    Unicode normalization forms. The two render identically, so they must
    group as one spelling — and a difference no reader can see must never
    itself be the flag."""
    r = find_inconsistencies(_paras(
        "The café-goers lingered over their cups long past the lunch hour.",
        unicodedata.normalize(
            "NFD", "Most café-goers knew better than to ask for a table."),
        "The cafégoers had claimed the terrace by the time she arrived."))
    assert list(_keys(r)) == ["cafégoers"]
    assert unicodedata.normalize("NFC", r.terms[0].dominant) == "café-goers"
    assert r.terms[0].minority_forms == ("cafégoers",)
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


# --- names: detect, decide, enforce --------------------------------------------
# A capitalized word that never appears lowercased and differs from another
# only in its diacritics is one name spelled two ways. Lopsided evidence is
# corrected as tracked changes; anything short of the bar is asked about.

def _name_paras(dominant=24, stray="Rian was late to the muster again."):
    texts = [f"Rían walked the wall at dusk on night {i} of the siege."
             for i in range(dominant)]
    if stray:
        texts.append(stray)
    return _paras(*texts)


def _drift(paras, **kw):
    return {n.key: n for n in find_name_drift(paras, **kw)}


def test_a_lopsided_diacritic_split_is_corrected():
    names = _drift(_name_paras())
    assert names["rian"].enforce
    assert names["rian"].dominant == "Rían"
    assert names["rian"].minority_forms == ("Rian",)
    assert len(names["rian"].outliers) == 1


def test_the_correction_is_a_tracked_change_finding():
    paras = _name_paras()
    report = find_inconsistencies(paras)
    fs = [f for f in to_findings(report, paras) if f.error_type == NAME_KEY]
    assert len(fs) == 1
    f = fs[0]
    assert f.finding_id.startswith("n-")           # its own id series
    assert f.original_text == "Rian was late to the muster again."
    assert f.corrected_text == "Rían was late to the muster again."
    assert "spells this name" in f.explanation


def test_a_word_that_appears_lowercase_is_ordinary_english():
    """exposé the noun against Expose the verb: any lowercase sighting means
    the word belongs to English, not to the cast list."""
    paras = _paras(
        "Exposé after exposé ran in the morning papers that season.",
        "Expose the ledger and the house of Vane falls with it.",
        "Expose them all, she said, and see what the city does then.")
    assert _drift(paras) == {}


def test_a_close_split_is_asked_about_not_corrected():
    paras = _paras(
        "Zoë kept the ledger. Zoë kept the keys. Zoë kept her own counsel.",
        "Zoe kept the garden gate locked after dark, whatever anyone said.")
    names = _drift(paras)
    assert not names["zoe"].enforce
    fs = to_findings(find_inconsistencies(paras), paras)
    queries = [f for f in fs if f.error_type == CONSISTENCY_KEY]
    assert len(queries) == 1
    assert queries[0].corrected_text == queries[0].original_text
    assert "Is the difference deliberate?" in queries[0].explanation


def test_a_thin_majority_is_asked_about():
    """Five-to-one dominance, but the name is barely established — a minor
    character seen ten times is not evidence enough to correct silently."""
    names = _drift(_name_paras(dominant=10))
    assert not names["rian"].enforce


def test_sharing_a_sentence_downgrades_to_a_question():
    """Two similarly-named characters interacting is exactly what the scan
    cannot tell from a slip, so it asks."""
    names = _drift(_name_paras(
        stray="Rían turned to Rian and neither spoke of the muster."))
    assert not names["rian"].enforce


def test_a_possessive_stray_is_corrected_without_touching_the_clitic():
    paras = _name_paras(stray="It was Rian’s watch when the beacon failed.")
    fs = [f for f in to_findings(find_inconsistencies(paras), paras)
          if f.error_type == NAME_KEY]
    assert fs[0].corrected_text == "It was Rían’s watch when the beacon failed."


def test_an_all_caps_stray_keeps_its_setting():
    paras = _name_paras(stray="RIAN AT THE WALL")
    fs = [f for f in to_findings(find_inconsistencies(paras), paras)
          if f.error_type == NAME_KEY]
    assert fs[0].corrected_text == "RÍAN AT THE WALL"


def test_case_only_difference_is_still_not_a_name_drift():
    """RIAN in a heading against Rian in prose is one spelling set two ways —
    the case-only rule holds for names just as it does for terms."""
    paras = _paras("RIAN AT THE WALL",
                   "Rian walked the wall at dusk, counting the torches.")
    assert _drift(paras) == {}


def test_names_scan_can_be_disabled():
    r = find_inconsistencies(_name_paras(), names=False)
    assert r.names == ()


def test_a_decomposed_manuscript_still_shows_its_name_drift():
    """Word writes composed accents, but text that passed through other tools
    can arrive decomposed; the scan must read Rían whole either way."""
    paras = _paras(*[unicodedata.normalize("NFD", t) for t in (
        [f"Rían walked the wall at dusk on night {i} of the siege."
         for i in range(24)] + ["Rian was late to the muster again."])])
    names = _drift(paras)
    assert names["rian"].enforce
    assert names["rian"].minority_forms == ("Rian",)
    assert unicodedata.normalize("NFC", names["rian"].dominant) == "Rían"


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


NAME_TEXTS = ([f"Rían walked the wall at dusk on night {i} of the siege."
               for i in range(24)]
              + ["Rian was late to the muster again."])


def test_a_run_corrects_the_stray_name(tmp_path):
    import zipfile
    # comments=True: the shipped default now quiets edit explanations for the
    # author's sake; this test is about the explanation machinery itself.
    _, out = _run(tmp_path, NAME_TEXTS, comments=True)
    assert out.applied == 1                      # the stray, and nothing else
    comments = zipfile.ZipFile(out.reviewed_path).read(
        "word/comments.xml").decode()
    assert "spells this name" in comments
    payload = json.loads(out.findings_json.read_text())
    names = payload["consistency"]["names"]
    assert names == [{"key": "rian", "dominant": "Rían",
                      "forms": {"Rían": 24, "Rian": 1},
                      "outliers": 1, "enforced": True}]
    assert payload["audit"]["passed"] is True
    assert "Names spelled more than one way" in out.summary_md.read_text()


def test_a_partial_run_makes_no_name_claim(tmp_path):
    prepared, _ = _run(tmp_path, NAME_TEXTS, max_chunks=1)
    assert prepared.consistency.names == ()


def test_the_shipped_config_enables_the_name_scan():
    cfg = load_config("config/default.yaml")
    assert cfg.consistency.names
    assert cfg.consistency.name_dominance == 5
    assert cfg.consistency.name_min_count == 20
    assert Config().consistency.names
