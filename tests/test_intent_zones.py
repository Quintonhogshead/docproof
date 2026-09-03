"""Executable intent zones (docproof/intent_zones.py): selectors resolve to
char spans, permission classes gate edit kinds, and the strictest zone wins on
overlap."""
from __future__ import annotations

import pytest

from docproof.intent_zones import (IntentZone, IntentZones, edit_kind_of,
                                    permits, resolve)
from docproof.models import ParagraphRef


def _paras():
    return [
        ParagraphRef("body-0001", "w", "body",
                     "For God so loved the world, that that Word came.",
                     "Normal"),
        ParagraphRef("body-0002", "w", "body",
                     'She said "grey" and he wrote gray.', "Normal"),
        ParagraphRef("body-0003", "w", "body",
                     "Plain author prose, fully editable here.", "Normal"),
    ]


# --- permission classes ------------------------------------------------------

def test_locked_forbids_every_edit_kind():
    z = IntentZone(label="s", permission="locked")
    assert permits(z, "wording") is False
    assert permits(z, "punctuation") is False


def test_punctuation_allows_only_punctuation():
    z = IntentZone(label="q", permission="punctuation")
    assert permits(z, "punctuation") is True
    assert permits(z, "wording") is False


def test_open_allows_everything():
    z = IntentZone(label="x", permission="open")
    assert permits(z, "wording") is True


def test_bad_permission_is_refused():
    with pytest.raises(ValueError, match="permission"):
        IntentZone(label="x", permission="whatever")


def test_edit_kind_classifies_punctuation_sweeps():
    assert edit_kind_of("sweep_ellipsis") == "punctuation"
    assert edit_kind_of("sweep_quote_punctuation") == "punctuation"
    assert edit_kind_of("homophone_confusion") == "wording"
    assert edit_kind_of("sweep_deity_capital") == "wording"   # not punctuation


# --- selectors ---------------------------------------------------------------

def test_para_ids_selector_protects_the_whole_paragraph():
    z = IntentZones(zones=[IntentZone(para_ids=["body-0001"])])
    r = resolve(z, _paras())
    assert r.zone_at("body-0001", 0, 5) is not None
    assert r.zone_at("body-0001", 40, 45) is not None
    assert r.zone_at("body-0002", 0, 5) is None


def test_terms_selector_protects_each_occurrence():
    z = IntentZones(zones=[IntentZone(terms=["that that"])])
    r = resolve(z, _paras())
    spans = r.as_dict()
    (lo, hi), = spans["body-0001"]
    assert _paras()[0].text[lo:hi] == "that that"


def test_regex_selector_resolves_matches():
    z = IntentZones(zones=[IntentZone(regex=r"gr[ae]y")])
    r = resolve(z, _paras())
    # "grey" and "gray" both in body-0002
    assert len(r.as_dict()["body-0002"]) == 2


def test_bad_regex_is_refused():
    with pytest.raises(ValueError, match="regex"):
        IntentZone(regex="(unclosed")


def test_quotes_selector_protects_quoted_text():
    z = IntentZones(zones=[IntentZone(quotes=True)])
    r = resolve(z, _paras())
    # the "grey" quoted span in body-0002
    assert "body-0002" in r.as_dict()


def test_para_range_selector_spans_reading_order():
    z = IntentZones(zones=[IntentZone(para_range=["body-0001", "body-0002"])])
    r = resolve(z, _paras())
    assert r.zone_at("body-0001", 0, 3) is not None
    assert r.zone_at("body-0002", 0, 3) is not None
    assert r.zone_at("body-0003", 0, 3) is None


def test_bad_para_range_length_is_refused():
    with pytest.raises(ValueError, match="para_range"):
        IntentZone(para_range=["only-one"])


# --- overlap: strictest wins -------------------------------------------------

def test_locked_beats_punctuation_on_overlap():
    z = IntentZones(zones=[
        IntentZone(label="wide", permission="punctuation", para_ids=["body-0001"]),
        IntentZone(label="tight", permission="locked", terms=["that that"]),
    ])
    r = resolve(z, _paras())
    # inside "that that": both zones cover it -> locked wins
    hit = r.zone_at("body-0001", 30, 34)
    assert hit is not None and hit.permission == "locked"
    # elsewhere in the paragraph: only the punctuation zone
    other = r.zone_at("body-0001", 0, 3)
    assert other is not None and other.permission == "punctuation"


def test_no_zones_means_nothing_protected():
    r = resolve(IntentZones(), _paras())
    assert r.any is False
    assert r.zone_at("body-0001", 0, 5) is None


# --- zero-width boundary insertions: title spans vs. TERM/regex zones -------
#
# The Purpura beta bug: appending a period to a LOCKED TITLE (a whole-line
# `para_ids` span) must still count as "inside" even though the insertion
# point is a zero-width span touching the zone's own end boundary. Redding
# then showed the SAME boundary-inclusive rule downgrading punctuation-only
# fixes that merely abut a regex TERM zone (a single word like "Mom" or
# "RESPOND") — the term itself was never touched, so that must NOT count as
# inside. `zone_at`'s `insert_text` keyword is what tells the two apart.

def test_title_period_still_counts_as_inside_a_whole_span_zone():
    """The regression test for the Purpura beta bug: a title zone is a
    `para_ids` (whole-line) span, so a period appended at its exact end
    boundary is still "inside" — insert_text is punctuation-only, but the
    zone is not term-shaped, so the old boundary-inclusive rule applies."""
    title = "The Wreck of the River Queen"          # len 29
    z = IntentZones(zones=[IntentZone(label="title", permission="locked",
                                      para_ids=["body-0010"])])
    paras = [ParagraphRef("body-0010", "w", "body", title, "Title")]
    r = resolve(z, paras)
    n = len(title)
    assert r.zone_at("body-0010", n, n, insert_text=".") is not None
    zone = r.zone_at("body-0010", n, n, insert_text=".")
    assert zone.permission == "locked"


def test_punctuation_insertion_adjacent_to_a_term_zone_is_not_inside():
    """A comma inserted right after a regex/terms zone's boundary must be
    permitted: the term ("Mom") is untouched."""
    z = IntentZones(zones=[IntentZone(label="term", permission="locked",
                                      terms=["Mom"])])
    paras = [ParagraphRef("body-0020", "w", "body", "Mom left early.",
                          "Normal")]
    r = resolve(z, paras)
    end = len("Mom")
    assert r.zone_at("body-0020", end, end, insert_text=",") is None


def test_letter_insertion_adjacent_to_a_term_zone_is_still_inside():
    """A letter appended right at a term's boundary changes the term itself
    ("Mom" -> "Moms") and must stay blocked."""
    z = IntentZones(zones=[IntentZone(label="term", permission="locked",
                                      terms=["Mom"])])
    paras = [ParagraphRef("body-0020", "w", "body", "Mom left early.",
                          "Normal")]
    r = resolve(z, paras)
    end = len("Mom")
    assert r.zone_at("body-0020", end, end, insert_text="s") is not None


def test_boundary_carve_out_needs_insert_text_to_apply():
    """Omitting `insert_text` keeps the old boundary-inclusive read for every
    zone, term-shaped or not — the safe default for a caller that has not
    been updated (e.g. a zero-width query with no known insertion text)."""
    z = IntentZones(zones=[IntentZone(label="term", permission="locked",
                                      terms=["Mom"])])
    paras = [ParagraphRef("body-0020", "w", "body", "Mom left early.",
                          "Normal")]
    r = resolve(z, paras)
    end = len("Mom")
    assert r.zone_at("body-0020", end, end) is not None


def test_enforce_permits_a_punctuation_only_fix_abutting_a_term_zone():
    """End to end through `enforce()`: a comma inserted right after a locked
    TERM zone ("RESPOND") must not be downgraded to a query — Redding's bug."""
    para = ParagraphRef("body-0030", "w", "body", "RESPOND now please.",
                        "Normal")
    f = _finding(finding_id="c-1", chunk_id="sweep", para_id="body-0030",
                 error_type="sweep_stacked_punctuation", occurrence=1,
                 original_text="RESPOND now please.",
                 corrected_text="RESPOND, now please.",
                 explanation="comma")
    z = IntentZones(zones=[IntentZone(label="term", permission="locked",
                                      terms=["RESPOND"])])
    kept, collisions = enforce([f], resolve(z, [para]), [para])
    assert kept[0].force_query is False
    assert collisions == []


def test_enforce_still_blocks_a_wording_edit_touching_a_term_zone():
    """An edit that actually rewrites the protected term still downgrades to
    a query — only the punctuation-boundary carve-out is new."""
    para = ParagraphRef("body-0030", "w", "body", "RESPOND now please.",
                        "Normal")
    f = _finding(finding_id="c-2", chunk_id="sweep", para_id="body-0030",
                 error_type="sweep_doubled_word", occurrence=1,
                 original_text="RESPOND now please.",
                 corrected_text="RESPONDS now please.",
                 explanation="wording")
    z = IntentZones(zones=[IntentZone(label="term", permission="locked",
                                      terms=["RESPOND"])])
    kept, collisions = enforce([f], resolve(z, [para]), [para])
    assert kept[0].force_query is True
    assert len(collisions) == 1


# --- enforcement (downgrade to query, never silent drop) ---------------------

from docproof.intent_zones import enforce
from docproof.models import Finding


def _finding(**kw):
    kw.setdefault("confidence", "high")
    return Finding(**kw)


def test_enforce_downgrades_a_wording_sweep_inside_a_locked_zone():
    para = _paras()[0]
    f = _finding(finding_id="dw-1", chunk_id="sweep", para_id="body-0001",
                 error_type="sweep_doubled_word", occurrence=1,
                 original_text=para.text,
                 corrected_text=para.text.replace("that that", "that"),
                 explanation="doubled")
    z = IntentZones(zones=[IntentZone(permission="locked",
                                      para_ids=["body-0001"])])
    kept, collisions = enforce([f], resolve(z, _paras()), _paras())
    assert kept[0].force_query is True
    assert len(collisions) == 1 and collisions[0]["permission"] == "locked"


def test_enforce_allows_a_punctuation_sweep_in_a_punctuation_zone():
    para = ParagraphRef("body-0009", "w", "body", "He paused...then went.",
                        "Normal")
    f = _finding(finding_id="el-1", chunk_id="sweep", para_id="body-0009",
                 error_type="sweep_ellipsis", occurrence=1,
                 original_text="He paused...then went.",
                 corrected_text="He paused…then went.", explanation="ell")
    z = IntentZones(zones=[IntentZone(permission="punctuation",
                                      para_ids=["body-0009"])])
    kept, collisions = enforce([f], resolve(z, [para]), [para])
    assert kept[0].force_query is False
    assert collisions == []


def test_enforce_blocks_a_wording_sweep_in_a_punctuation_zone():
    para = ParagraphRef("body-0009", "w", "body", "the word here", "Normal")
    f = _finding(finding_id="w-1", chunk_id="sweep", para_id="body-0009",
                 error_type="sweep_deity_capital", occurrence=1,
                 original_text="the word here",
                 corrected_text="the Word here", explanation="cap")
    z = IntentZones(zones=[IntentZone(permission="punctuation",
                                      para_ids=["body-0009"])])
    kept, _ = enforce([f], resolve(z, [para]), [para])
    assert kept[0].force_query is True


def test_enforce_leaves_findings_outside_zones_untouched():
    para = _paras()[2]      # body-0003, no zone
    f = _finding(finding_id="x-1", chunk_id="sweep", para_id="body-0003",
                 error_type="sweep_doubled_word", occurrence=1,
                 original_text=para.text, corrected_text=para.text + "!",
                 explanation="x")
    z = IntentZones(zones=[IntentZone(permission="locked",
                                      para_ids=["body-0001"])])
    kept, collisions = enforce([f], resolve(z, _paras()), _paras())
    assert kept[0].force_query is False
    assert collisions == []


def test_enforce_no_zones_is_a_noop():
    para = _paras()[0]
    f = _finding(finding_id="x", chunk_id="sweep", para_id="body-0001",
                 error_type="sweep_doubled_word", occurrence=1,
                 original_text=para.text, corrected_text=para.text,
                 explanation="x")
    kept, collisions = enforce([f], resolve(IntentZones(), _paras()), _paras())
    assert kept == [f] and collisions == []
