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
    # These tests isolate name/term consistency; the residual number pass would
    # otherwise spell out incidental prose numbers in the fixtures ("night 5")
    # and inflate the applied count with changes that have nothing to do with
    # the behaviour under test.
    cfg.residuals.enabled = False
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


# --- mechanical scans: spelling variants (VarCon) -----------------------------
# grey/gray differ by a letter, not by hyphenation, so the compound key scan
# cannot group them. These read the whole book, count every spelling, and only
# ask. The negatives matter most: a name, an author-owned word, and a form the
# variant already enforces must never be raised.

def _variants(report):
    return {g.key: g for g in report.variants}


def test_a_letter_level_spelling_variant_is_flagged():
    r = find_inconsistencies(_paras(
        "The colour of the water changed with the light over the bay.",
        "She painted the colour of the sky the way she remembered it.",
        "He could not name the color of her eyes when they asked him."))
    g = _variants(r)["color"]
    assert dict(g.counts) == {"colour": 2, "color": 1}
    assert g.dominant == "colour" and g.has_majority


def test_the_recommendation_is_the_form_the_book_uses_most():
    r = find_inconsistencies(_paras(
        "They organize the archive by year and then by author surname.",
        "We organize the shelves twice a year, before and after the fair.",
        "The society will organise a gala to mark the centenary this spring."))
    assert _variants(r)["organize"].dominant == "organize"


def test_a_spelling_variant_query_changes_nothing_and_cites_chicago():
    paras = _paras(
        "The colour of the water changed with the light over the bay.",
        "She painted the colour of the sky the way she remembered it.",
        "He could not name the color of her eyes when they asked him.")
    findings = [f for f in to_findings(find_inconsistencies(paras), paras)
                if "spells one word" in f.explanation]
    assert len(findings) == 1
    f = findings[0]
    assert f.error_type == CONSISTENCY_KEY
    assert f.corrected_text == f.original_text          # a query edits nothing
    assert "Merriam-Webster" in f.explanation           # Chicago note rides along


def test_chicago_notes_can_be_turned_off():
    paras = _paras(
        "The colour of the water changed with the light over the bay.",
        "She painted the colour of the sky the way she remembered it.",
        "He could not name the color of her eyes when they asked him.")
    r = find_inconsistencies(paras, chicago_notes=False)
    assert r.variants[0].note == ""


def test_an_even_spelling_split_asks_which_to_settle_on():
    r = find_inconsistencies(_paras(
        "He walked toward the gate, slow and unwilling, in the grey dawn.",
        "She turned towards the sea and would not look back at the house."))
    paras = _paras(
        "He walked toward the gate, slow and unwilling, in the grey dawn.",
        "She turned towards the sea and would not look back at the house.")
    r = find_inconsistencies(paras)
    g = _variants(r)["toward"]
    assert not g.has_majority
    assert any("settle on" in f.explanation for f in to_findings(r, paras))


def test_a_name_is_not_a_spelling_variant():
    """Mr. Grey is a character, not the colour spelled the British way."""
    r = find_inconsistencies(_paras(
        "Mr. Grey wore a grey coat to the funeral in the rain that morning.",
        "Mr. Grey said nothing to anyone as the gray sky pressed down low.",
        "Mr. Grey left before the reading of the will could even begin."))
    # 'Grey' occurrences are names (capitalized mid-sentence); only 'grey'/'gray'
    # the colour remain, one each — an even split, still a single query, never
    # counting the surname.
    g = _variants(r).get("gray")
    if g is not None:
        assert "Grey" not in g.counts


def test_an_enforced_variant_is_not_also_asked_about():
    """On a U.S. run grey->gray rides the respell map, so adjudication converts
    it; the consistency scan must not query the same site."""
    paras = _paras(
        "The grey sky pressed low over the grey and empty winter fields.",
        "A gray coat hung by the door where the gray light could not reach.")
    assert "gray" not in _variants(find_inconsistencies(
        paras, respell={"grey": "gray"}))
    assert "gray" in _variants(find_inconsistencies(paras, respell={}))


def test_an_author_owned_word_is_not_a_spelling_variant():
    paras = _paras(
        "The Colour was a place, not a shade: the Colour swallowed light.",
        "They feared the Colour. The colour of ordinary things was safe.")
    # 'Colour' the coined proper noun is protected; the lone lowercase 'colour'
    # then stands alone and nothing is raised.
    assert "color" not in _variants(find_inconsistencies(
        paras, protected=["Colour"]))


def test_a_consistent_book_raises_no_spelling_variant():
    r = find_inconsistencies(_paras(
        "The color of the water changed with the light over the bay at dusk.",
        "She painted the color of the sky the way she remembered it as a child.",
        "He could not name the color of her eyes when they finally asked him."))
    assert r.variants == ()


# --- the shipped table --------------------------------------------------------

def test_the_varcon_table_is_present_and_excludes_homographs():
    from docproof.consistency import _load_varcon
    forms, members = _load_varcon()
    assert forms, "varcon.tsv did not load"
    # The headline variants are grouped.
    assert forms.get("colour") == "color" and forms.get("gray") == "gray"
    assert forms.get("towards") == "toward"
    # Different words by sense must never appear — flagging these is the one way
    # the scan fails. See tools/build_varcon.py::_EXCLUDED.
    for danger in ("storey", "cheque", "tyre", "metre", "programme",
                   "practise", "licence", "analyses"):
        assert danger not in forms, f"{danger} leaked into the variant table"


# --- mechanical scans: abbreviations ------------------------------------------

def _abbrevs(report):
    return {g.key: g for g in report.abbreviations}


def test_a_dotted_and_undotted_abbreviation_is_flagged():
    r = find_inconsistencies(_paras(
        "The U.S. delegation arrived first, ahead of the U.S. press corps.",
        "Later the US ambassador spoke about the treaty and its terms."))
    g = _abbrevs(r)["us"]
    assert g.dominant == "U.S." and g.has_majority
    assert dict(g.counts) == {"U.S.": 2, "US": 1}


def test_a_pronoun_and_a_verb_never_join_an_abbreviation_group():
    """'us' and 'am' are lowercase, so they cannot be pulled into the U.S. and
    a.m. groups — the guard is structural, not a word list."""
    r = find_inconsistencies(_paras(
        "Give the report to us before nine. I am certain it matters to us.",
        "The meeting is at 9 a.m., and the second one runs to 5 p.m. sharp."))
    assert _abbrevs(r) == {}


def test_spaced_personal_initials_are_not_an_abbreviation():
    r = find_inconsistencies(_paras(
        "J. R. R. Tolkien wrote it slowly, over years, in his spare hours.",
        "J. R. R. Tolkien revised it again before he would let anyone read."))
    assert _abbrevs(r) == {}


def test_a_heading_in_capitals_is_not_abbreviation_drift():
    paras = [ParagraphRef("body-0000", "word/document.xml", "body",
                          "THE US TREATY", "Heading1"),
             ParagraphRef("body-0001", "word/document.xml", "body",
                          "The U.S. team signed the U.S. treaty that spring.",
                          "Normal")]
    assert _abbrevs(find_inconsistencies(paras)) == {}


def test_one_abbreviation_style_alone_is_silent():
    r = find_inconsistencies(_paras(
        "The US team won the US title and then the US crowd went home.",
        "The US press said little about the US victory the next morning."))
    assert r.abbreviations == ()


# --- mechanical scans: acronym case -------------------------------------------

def _cases(report):
    return {g.key: g for g in report.casings}


def test_an_acronym_set_two_ways_is_flagged():
    r = find_inconsistencies(_paras(
        "NASA announced the mission. NASA had waited years for the window.",
        "Later Nasa confirmed the crew and the launch date to the reporters."),
        dictionary="en_US")
    g = _cases(r)["nasa"]
    assert g.dominant == "NASA" and g.has_majority
    assert dict(g.counts) == {"NASA": 2, "Nasa": 1}


def test_an_ordinary_word_is_not_acronym_drift():
    """AIDS the initialism and 'aids' the verb share letters; because 'aids' is
    a dictionary word the scan leaves the whole key alone."""
    r = find_inconsistencies(_paras(
        "The AIDS ward was full that winter and the AIDS clinic overflowed.",
        "A good map aids the traveler. Aids to navigation lined the channel."),
        dictionary="en_US")
    assert "aids" not in _cases(r)


def test_a_protected_name_shouted_in_caps_is_not_acronym_drift():
    """A character whose name the spell scan protects can be shouted in dialogue
    ("VORREN!") without the caps reading as drift from the ordinary spelling."""
    r = find_inconsistencies(_paras(
        "“VORREN!” she screamed across the yard as the roof began to fall.",
        "“VORREN!” came the cry again, closer now, from somewhere in the smoke.",
        "Vorren turned at the sound of his name and could not find her face."),
        dictionary="en_US", protected=["Vorren"])
    assert "vorren" not in _cases(r)
    # …and without the protection it would be raised, so the guard is doing work.
    r2 = find_inconsistencies(_paras(
        "“VORREN!” she screamed across the yard as the roof began to fall.",
        "“VORREN!” came the cry again, closer now, from somewhere in the smoke.",
        "Vorren turned at the sound of his name and could not find her face."),
        dictionary="en_US")
    assert "vorren" in _cases(r2)


def test_acronym_case_declines_without_a_dictionary():
    r = find_inconsistencies(_paras(
        "NASA announced the mission. NASA had waited years for the window.",
        "Later Nasa confirmed the crew and the launch date to the reporters."),
        dictionary="nonexistent_variant_xx")
    assert r.casings == ()


def test_only_all_caps_with_no_word_form_is_silent():
    r = find_inconsistencies(_paras(
        "NASA announced the mission. NASA had waited years for the window.",
        "NASA confirmed the crew and the launch date later to the reporters."),
        dictionary="en_US")
    assert r.casings == ()


# --- cross-cutting ------------------------------------------------------------

_MIXED = (
    "The colour of the U.S. flag, NASA said, was true. He walked toward it.",
    "She painted the color of the sky and turned towards the grey US shore.",
    "Later Nasa spoke to the US press. NASA denied the gray of the smoke.")


def test_every_mechanical_finding_is_a_query():
    paras = _paras(*_MIXED)
    r = find_inconsistencies(paras, dictionary="en_US")
    mech = [f for f in to_findings(r, paras)
            if f.para_id and f.finding_id.startswith("c-")]
    assert mech
    for f in mech:
        assert f.corrected_text == f.original_text


def test_finding_ids_are_unique_across_all_kinds():
    paras = _paras(*_MIXED)
    r = find_inconsistencies(paras, dictionary="en_US")
    ids = [f.finding_id for f in to_findings(r, paras)]
    assert len(ids) == len(set(ids))


def test_the_scan_is_deterministic():
    paras = _paras(*_MIXED)
    a = [f.explanation for f in to_findings(
        find_inconsistencies(paras, dictionary="en_US"), paras)]
    b = [f.explanation for f in to_findings(
        find_inconsistencies(paras, dictionary="en_US"), paras)]
    assert a == b and len(a) >= 4


def test_the_mixed_sample_exercises_all_three_scans():
    r = find_inconsistencies(_paras(*_MIXED), dictionary="en_US")
    assert r.variants and r.abbreviations and r.casings


def test_the_per_kind_cap_bounds_the_output():
    # Two clusters, cap of one: only one spelling-variant query survives, and
    # the drop is logged, never silent.
    paras = _paras(
        "The colour and the flavour were both wrong that evening at dinner.",
        "The color and the flavor were right the next day when she tried.")
    r = find_inconsistencies(paras, max_queries_per_kind=1)
    assert len(r.variants) == 1


def test_flagged_counts_mechanical_groups():
    paras = _paras(*_MIXED)
    r = find_inconsistencies(paras, dictionary="en_US")
    assert r.flagged == (
        sum(len(t.outliers) for t in r.terms)
        + sum(len(n.outliers) for n in r.names if not n.enforce)
        + len(r.variants) + len(r.abbreviations) + len(r.casings))


def test_the_shipped_config_enables_the_mechanical_scans():
    cfg = load_config("config/default.yaml")
    assert cfg.consistency.spelling_variants
    assert cfg.consistency.abbreviations
    assert cfg.consistency.acronym_case
    assert cfg.consistency.chicago_notes
    assert cfg.consistency.max_queries_per_kind == 40
    assert Config().consistency.spelling_variants


# --- deity pronouns (the Grenada memoir's "and he knows") ---------------------

def _reverent_paras():
    filler = ("God is patient with us! He also sees the struggles we go "
              "through, for His mercy endures and Him alone we praise, "
              "and Himself provides; He watches, His hand guides, Him we "
              "trust, He remains.")
    return _paras(
        filler,
        "The Lord watches; His hand guides us and Jesus walks beside us.",
        "God is patient with us! He also sees the struggles and challenges "
        "we go through, and he knows every decision we make.",
    )


def test_deity_pronoun_stray_is_queried():
    from docproof.consistency import find_deity_pronouns
    drift = find_deity_pronouns(_reverent_paras(), min_capitalized=8)
    assert drift is not None
    assert [o.form for o in drift.outliers] == ["he"]
    findings = to_findings(
        find_inconsistencies(_reverent_paras(), min_length=7), _reverent_paras())
    deity = [f for f in findings if "pronouns referring to God" in f.explanation]
    assert len(deity) == 1
    assert deity[0].corrected_text == deity[0].original_text     # query only


def test_deity_scan_stays_silent_without_the_convention():
    from docproof.consistency import find_deity_pronouns
    secular = _paras(
        "He walked home and he thought about the game.",
        "She told him the news and his face fell.",
    )
    assert find_deity_pronouns(secular) is None


# --- the US variant policy (theatre -> theater) --------------------------------

def test_variant_policy_queries_a_consistently_british_form():
    paras = _paras(
        "They met at the theatre after the show closed.",
        "The theatre was dark by nine, and the theatre sign was off.",
    )
    report = find_inconsistencies(paras, variant_policy="us")
    assert [g.dominant for g in report.policy] == ["theater"]
    findings = to_findings(report, paras)
    policy = [f for f in findings if "U.S. spelling" in f.explanation]
    assert len(policy) == 1 and "theater" in policy[0].explanation
    assert policy[0].corrected_text == policy[0].original_text   # query only


def test_variant_policy_off_by_default_and_skips_mixed_and_accepted():
    paras = _paras(
        "They met at the theatre after the show.",
        "The theater was dark; he walked towards the door.",
    )
    assert find_inconsistencies(paras).policy == ()
    report = find_inconsistencies(paras, variant_policy="us")
    # Mixed theatre/theater is the mixed-usage scan's; towards is accepted US.
    assert report.policy == ()


# --- clock-time style (bare hours in an H:MM book) -----------------------------

def _time_paras():
    return _paras(
        "He calls me around 2 p.m. on a Saturday to ask about plans.",
        "Around 11:00 a.m., she shows signs of life.",
        "The alarm was set for 6:30 and again for 7:15.",
        "Someone started the meeting at 8. It starts at 8:10, dummy.",
        "We will start getting ready around 4.",
    )


def test_time_style_flags_bare_hours_in_an_hmm_book():
    from docproof.consistency import find_time_style
    drift = find_time_style(_time_paras())
    assert drift is not None and drift.with_minutes >= 3
    assert sorted(o.form for o in drift.outliers) == ["2", "4", "8"]


def test_time_style_stays_quiet_without_hmm_evidence():
    from docproof.consistency import find_time_style
    paras = _paras(
        "We will start getting ready around 4.",
        "Someone started the meeting at 8.",
    )
    assert find_time_style(paras) is None


def test_time_style_skips_quantities_and_spelled_styles():
    from docproof.consistency import find_time_style
    paras = _paras(
        "It was 6:30, then 7:15, then 11:00 before she stirred.",
        "I waited 10 minutes and left by 5 percent margins at 2 o'clock.",
        "He arrived after 3 days at 14 Elm Street.",
    )
    assert find_time_style(paras) is None


def test_open_vs_hyphen_compound_verb_noun_split_is_not_flagged():
    # P1-7: "check in" (phrasal verb) and "check-in" (noun) are two correct
    # forms, not one term spelled two ways. The part-of-speech gate suppresses
    # the query the term scan used to raise.
    paras = _paras(
        "Please check in at the desk when you arrive.",
        "You should check in early for the flight.",
        "We reached the check-in desk at noon.",
        "The check-in was slow that morning.",
    )
    report = find_inconsistencies(paras)
    keys = {t.key for t in report.terms}
    assert "checkin" not in keys


def test_a_genuine_compound_typo_is_still_flagged():
    # No verb/noun split here — one spelling simply dominates, so it stays a
    # query. "underway" (x3) vs a lone "under way" reads as a slip.
    paras = _paras(
        "The project is underway.",
        "Repairs are underway now.",
        "Construction is underway again.",
        "The plan is under way at last.",
    )
    report = find_inconsistencies(paras)
    assert any(t.key == "underway" for t in report.terms)


def test_time_style_findings_are_one_book_level_query():
    # P1-7: bare clock hours are ONE book-level question, not one per site — the
    # decision (write :00 on bare hours?) is a single style call for the book.
    report = find_inconsistencies(_time_paras())
    findings = to_findings(report, _time_paras())
    times = [f for f in findings if "clock times with minutes" in f.explanation]
    assert len(times) == 1
    (f,) = times
    assert f.corrected_text == f.original_text              # query only
    assert ":00" in f.explanation
    assert "stand bare" in f.explanation                    # names the count


# --- accented loanwords (Si -> Sí) ---------------------------------------------

def test_accent_loanword_queried_at_first_bare_occurrence():
    paras = _paras(
        "Without hesitation, I reply, “Si!”",
        "I respond with another, “Sí!” FAHK!",
    )
    report = find_inconsistencies(paras)
    assert [g.key for g in report.accents] == ["sí"]
    findings = to_findings(report, paras)
    accent = [f for f in findings if "loanword" in f.explanation]
    assert len(accent) == 1
    assert "sí" in accent[0].explanation
    assert accent[0].corrected_text == accent[0].original_text   # query only


def test_accent_loanword_respects_protection_and_accented_books():
    from docproof.consistency import find_accent_loanwords
    # "Si" is a character's name: protected, no query.
    paras = _paras("Si nodded at the counter and said nothing.")
    assert find_accent_loanwords(paras, protected=("Si",)) == ()
    # A book that always writes the accent has nothing to ask about.
    assert find_accent_loanwords(_paras("“Sí!” she said.")) == ()
    # An ordinary book mentions none of the table's words.
    assert find_accent_loanwords(_paras("The theater was dark.")) == ()
