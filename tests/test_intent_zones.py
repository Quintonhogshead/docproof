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
