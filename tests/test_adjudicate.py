"""The candidate-adjudication pass: deterministic generation, then the model's
verdicts turned into correctly-routed findings."""
from __future__ import annotations

import itertools

from docproof.adjudicate import (Candidate, adjudicate, edits1, generate,
                                 _match_case, _sentence_around)
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


# --- generation ---------------------------------------------------------------

def test_edits1_is_the_one_edit_neighbourhood():
    e = edits1("cat")
    assert "at" in e and "cot" in e and "cart" in e and "act" in e
    assert "cat" not in e                      # never itself


def test_a_nonword_next_to_a_common_word_is_a_typo_candidate():
    paras = [_para("body-0", "I just staired at the wall for a while.")]
    cands = generate(paras)
    words = {c.word: c for c in cands}
    assert "staired" in words
    assert words["staired"].kind == "typo"


def test_a_plain_correct_word_is_never_a_candidate():
    # "stared" is a common word; nothing one edit away should drag it in.
    paras = [_para("body-0", "She stared at the door and said nothing at all.")]
    assert [c for c in generate(paras) if c.word == "stared"] == []


def test_a_protected_misspelling_with_a_common_twin_is_a_near_miss():
    # "gonig" is protected (author's own, per the caller) but sits one edit from
    # the far-commoner "going" — exactly the repeated-typo the lexicon overshields.
    paras = [_para("body-0", "We were gonig home when the gonig got strange.")]
    cands = generate(paras, protected=["gonig"])
    kinds = {c.kind for c in cands if c.word == "gonig"}
    assert kinds == {"near_miss"}


def test_a_genuine_coinage_stays_protected():
    # An invented word with no common twin must never surface, even protected.
    paras = [_para("body-0", "The Xylthenar rose over Xylthenar valley at dawn.")]
    assert [c for c in generate(paras, protected=["xylthenar"]) if c.kind == "near_miss"] == []


def test_every_occurrence_becomes_its_own_candidate():
    paras = [_para("body-0", "I staired, then staired again at the staired wall.")]
    cands = [c for c in generate(paras) if c.word == "staired"]
    assert len(cands) == 3                      # one judgement, three fix sites
    assert all(paras[0].text[c.start:c.end].lower() == "staired" for c in cands)


def test_max_candidates_caps_the_work():
    paras = [_para(f"body-{i}", "He staired at it.") for i in range(20)]
    assert len(generate(paras, max_candidates=5)) == 5


def test_a_denylisted_spelling_is_ruled_on_with_its_given_fix():
    # 'alot' has no real word one edit away, so the typo signal would miss it;
    # the denylist forces the ruling and supplies the two-word correction.
    paras = [_para("body-0", "There was alot to do before the alot ran out.")]
    cands = [c for c in generate(paras, denylist={"alot": "a lot"})
             if c.word == "alot"]
    assert len(cands) == 2                          # every occurrence, one fix
    assert {c.kind for c in cands} == {"denylist"}
    assert {c.suggestion for c in cands} == {"a lot"}


def test_the_denylist_beats_protection():
    # Even a word the caller protected as the author's own yields to the house
    # denylist — protection is for coinages, not for spellings the press bans.
    paras = [_para("body-0", "we sat aswell aswell by the fire that night.")]
    cands = [c for c in generate(paras, protected=["aswell"],
                                 denylist={"aswell": "as well"})]
    assert cands and all(c.kind == "denylist" for c in cands)


# --- helpers ------------------------------------------------------------------

def test_match_case_follows_the_source():
    assert _match_case("Staired", "stared") == "Stared"
    assert _match_case("STAIRED", "stared") == "STARED"
    assert _match_case("staired", "stared") == "stared"


def test_sentence_around_isolates_the_containing_sentence():
    text = "First one. The best swordsmen in the city. Then a third."
    s = _sentence_around(text, text.index("swordsmen"), text.index("swordsmen") + 9)
    assert s == "The best swordsmen in the city."


# --- adjudication + routing ---------------------------------------------------

def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


def test_a_high_confidence_correction_becomes_an_edit_finding():
    paras = [_para("body-0", "I just staired at it.")]
    cand = Candidate("body-0", "staired", 7, 14, "stained", "typo")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "correction": "stared", "confidence": "high"})])
    out = adjudicate([cand], paras, prov, model="m", max_tokens=100,
                     usage=Usage(), ids=itertools.count(1))
    assert len(out) == 1
    f = out[0]
    assert f.error_type == "spelling" and f.confidence == "high"
    assert f.corrected_text == "I just stared at it."   # model's word, not the hint
    assert f.force_query is False                        # trusted as an edit


def test_a_soft_correction_is_routed_to_a_query_not_an_edit():
    paras = [_para("body-0", "He wore his fancy armoire to the ball.")]
    cand = Candidate("body-0", "armoire", 12, 19, "armory", "typo")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "correction": "armor", "confidence": "medium"})])
    out = adjudicate([cand], paras, prov, model="m", max_tokens=100,
                     usage=Usage(), ids=itertools.count(1))
    assert len(out) == 1 and out[0].force_query is True   # asks, never silently edits


def test_a_kept_word_produces_nothing():
    paras = [_para("body-0", "The charnel house stood at the wood's edge.")]
    cand = Candidate("body-0", "charnel", 4, 11, "channel", "typo")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": False, "correction": "", "confidence": "high"})])
    assert adjudicate([cand], paras, prov, model="m", max_tokens=100,
                      usage=Usage(), ids=itertools.count(1)) == []


def test_a_noop_correction_is_dropped():
    paras = [_para("body-0", "I just stared at it.")]
    cand = Candidate("body-0", "stared", 7, 13, "stared", "typo")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "correction": "stared", "confidence": "high"})])
    assert adjudicate([cand], paras, prov, model="m", max_tokens=100,
                      usage=Usage(), ids=itertools.count(1)) == []


def test_edit_confidence_knob_can_demand_only_the_surest_edits():
    paras = [_para("body-0", "I just staired at it.")]
    cand = Candidate("body-0", "staired", 7, 14, "stared", "typo")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "correction": "stared", "confidence": "medium"})])
    # With the floor at high, a medium verdict must be a query, not an edit.
    out = adjudicate([cand], paras, prov, model="m", max_tokens=100,
                     usage=Usage(), ids=itertools.count(1), edit_confidence="high")
    assert out[0].force_query is True


# --- pipeline wiring ----------------------------------------------------------

def _docx_with(paragraphs, path):
    import docx
    d = docx.Document()
    for t in paragraphs:
        d.add_paragraph(t)
    d.save(str(path))
    return str(path)


def test_prepare_populates_candidates_on_a_whole_document_run(tmp_path):
    from docproof.config import load_config
    from docproof.pipeline import prepare
    doc = _docx_with(
        ["The knight drew his blade and staired at the dark horizon ahead.",
         "He staired again, then turned and walked back toward the camp."],
        tmp_path / "typo.docx")
    cfg = load_config("config/default.yaml")
    prep = prepare(cfg, doc, "config/error_types")
    words = {c.word for c in prep.adjudicate_candidates}
    assert "staired" in words


def test_prepare_skips_candidates_when_the_pass_is_off(tmp_path):
    from docproof.config import load_config
    from docproof.pipeline import prepare
    doc = _docx_with(
        ["The knight drew his blade and staired at the dark horizon ahead."],
        tmp_path / "typo.docx")
    cfg = load_config("config/default.yaml")
    cfg.adjudicate.enabled = False
    prep = prepare(cfg, doc, "config/error_types")
    assert prep.adjudicate_candidates == []


# --- a word under review is no longer protected -------------------------------

def test_a_near_miss_candidate_leaves_the_protected_lexicon():
    # The whole point of a near-miss is that a PROTECTED word is being questioned
    # as a repeated typo; keeping it in the lexicon would tell every judge to
    # protect and to doubt the same word at once.
    from docproof.pipeline import _unprotect_near_miss
    from docproof.spellscan import SpellScan
    spell = SpellScan(lexicon=("Annastasia", "Kaelith"))
    cands = [Candidate("body-0", "annastasia", 0, 10, "anastasia", "near_miss")]
    out = _unprotect_near_miss(spell, cands)
    assert "Annastasia" not in out.lexicon      # under review, so no longer owned
    assert "Kaelith" in out.lexicon             # a true coinage is untouched


def test_typo_and_denylist_candidates_do_not_disturb_the_lexicon():
    # Only near-misses come out of the lexicon: a typo or a denylisted spelling
    # was never protected in the first place.
    from docproof.pipeline import _unprotect_near_miss
    from docproof.spellscan import SpellScan
    spell = SpellScan(lexicon=("Kaelith",))
    cands = [Candidate("body-0", "staired", 0, 7, "stared", "typo"),
             Candidate("body-0", "alot", 8, 12, "a lot", "denylist")]
    assert _unprotect_near_miss(spell, cands).lexicon == ("Kaelith",)
