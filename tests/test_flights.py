"""The copy-edit flights lane: lens prompts, siting/filtering, transitive
clustering, both judge postures, the accept floor, and the findings envelope.

No test in this file makes a network call — every provider is a
`tests.fakes.FakeProvider` replaying scripted `ProviderResult`s, following the
same convention as tests/test_smoothing.py and tests/test_rewrite.py.
"""
from __future__ import annotations

import itertools
import json

import docproof.flights as fl
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str, *, reviewable: bool = True) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal", reviewable=reviewable)


def _suggest(*items) -> ProviderResult:
    return ProviderResult(parsed={"suggestions": list(items)})


def _verdict(**kw) -> ProviderResult:
    base = {"verdict": "keep", "chosen_index": -1, "chosen_text": "",
           "confidence": "medium", "reason": ""}
    base.update(kw)
    return ProviderResult(parsed=base)


def _prop(para_id="body-0001", quote="foo", replacement="bar",
         rationale="reads better") -> dict:
    return {"para_id": para_id, "quote": quote, "replacement": replacement,
           "rationale": rationale}


# --- lens prompt assembly ----------------------------------------------------

def test_lens_system_assembles_header_brief_footer():
    text = fl.lens_system("economy")
    assert text.startswith(fl.COMMON_HEADER)
    assert fl.LENS_BRIEFS["economy"] in text
    assert text.endswith(fl.COMMON_FOOTER)


def test_lens_system_unknown_lens_falls_back_to_general():
    assert fl.lens_system("not-a-real-lens") == fl.lens_system("general")


def test_all_six_default_lenses_have_distinct_briefs():
    briefs = {fl.LENS_BRIEFS[name] for name in fl.LENSES}
    assert len(briefs) == len(fl.LENSES) == 6


# --- heading skip is structural, not a corpus-specific regex -----------------

def test_usable_paragraphs_excludes_non_reviewable_regardless_of_text():
    paras = [
        _para("body-0001", "Chapter One", reviewable=False),
        _para("body-0002", "Some ordinary narrative sentence.", reviewable=True),
        _para("body-0003", "   ", reviewable=True),  # blank after strip
    ]
    usable = fl.usable_paragraphs(paras)
    assert [p.para_id for p in usable] == ["body-0002"]


# --- siting / filtering: one drop reason at a time ---------------------------

def test_site_and_filter_drops_unknown_para_id():
    text_of = {"body-0001": "Hello there."}
    cands, dropped = fl.site_and_filter(
        [("body-9999", "Hello", "Hi", "shorter")], text_of,
        closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped == 1


def test_site_and_filter_drops_empty_quote():
    text_of = {"body-0001": "Hello there."}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "", "Hi", "r")], text_of,
        closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped == 1


def test_site_and_filter_drops_unanchored_quote():
    text_of = {"body-0001": "Hello there."}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "not in the paragraph", "Hi", "r")], text_of,
        closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped == 1


def test_site_and_filter_drops_identity_replacement():
    text_of = {"body-0001": "Hello there."}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "Hello", "Hello", "no-op")], text_of,
        closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped == 1


def test_site_and_filter_drops_dialogue_overlap():
    text = 'She said, “Hello there, friend.” and left.'
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "Hello there, friend", "Hi there, pal", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped == 1


def test_site_and_filter_keeps_narration_outside_dialogue():
    text = 'She said, “Hello there.” It was really quite a long walk.'
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "really quite a long walk", "a long walk", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert dropped == 0 and len(cands) == 1
    assert cands[0].replacement == "a long walk"


def test_site_and_filter_drops_exact_duplicate():
    text_of = {"body-0001": "It was really quite obviously a mistake."}
    rows = [("body-0001", "really quite obviously", "clearly", "r1"),
            ("body-0001", "really quite obviously", "clearly", "r2")]
    cands, dropped = fl.site_and_filter(
        rows, text_of, closing_quotes="”\"", model="m", lens="economy")
    assert len(cands) == 1 and dropped == 1


def test_site_and_filter_keeps_two_distinct_rewrites_of_same_span():
    text_of = {"body-0001": "It was really quite obviously a mistake."}
    rows = [("body-0001", "really quite obviously", "clearly", "r1"),
            ("body-0001", "really quite obviously", "plainly", "r2")]
    cands, dropped = fl.site_and_filter(
        rows, text_of, closing_quotes="”\"", model="m", lens="economy")
    assert len(cands) == 2 and dropped == 0


# --- drop reasons are NAMED, not just counted --------------------------------

def test_site_and_filter_names_the_classic_drop_reasons():
    text = 'She said, “Hello there, friend.” It was obviously a mistake.'
    text_of = {"body-0001": text}
    rows = [
        ("body-9999", "Hello", "Hi", "unknown para"),
        ("body-0001", "", "Hi", "empty quote"),
        ("body-0001", "not in the paragraph at all", "Hi", "unanchored"),
        ("body-0001", "obviously", "obviously", "no-op"),
        ("body-0001", "Hello there, friend", "Hi there, pal", "dialogue"),
        ("body-0001", "obviously", "plainly", "kept"),
        ("body-0001", "obviously", "plainly", "duplicate"),
    ]
    cands, dropped = fl.site_and_filter(
        rows, text_of, closing_quotes="”\"", model="m", lens="economy")
    assert len(cands) == 1
    assert dropped.reasons == {"unknown_para": 2, "unanchored": 1, "no_op": 1,
                              "dialogue": 1, "duplicate": 1}
    assert dropped.total == 6


def test_drop_counts_still_reads_as_the_plain_int_it_replaced():
    """Every existing call site (docproof/__main__.py prints it, tests compare
    it) treats the second return value as an int — it must keep behaving as
    one."""
    dropped = fl.DropCounts(reasons={"no_op": 2, "dialogue": 1}, stripped=3)
    assert dropped == 3
    assert dropped != 4
    assert int(dropped) == 3
    assert bool(dropped) is True
    assert not fl.DropCounts()
    assert 10 + dropped == 13 and dropped + 10 == 13
    assert dropped > 2 and dropped <= 3
    assert f"{dropped} dropped" == "3 dropped"
    assert dropped.summary() == "no_op=2, dialogue=1"
    assert dropped.to_json() == {"total": 3,
                                 "reasons": {"no_op": 2, "dialogue": 1},
                                 "notes_stripped": 3}


# --- malformed: the replacement does not cover the span it quotes ------------

def test_site_and_filter_drops_malformed_head_at_a_paragraph_start():
    """The shipped Redding failure: the quote begins the sentence with a
    capital and the replacement begins lowercase, so applying it decapitated
    the sentence and left a fragment."""
    text = "It made me really stop and think about how the world works."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "It made me really stop and think about how",
          "stop and think about how", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert cands == []
    assert dropped.reasons == {"malformed_head": 1}


def test_site_and_filter_drops_malformed_head_after_a_sentence_boundary():
    text = "She waited a while. It made me stop and think about it."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "It made me stop", "stop", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped.reasons == {"malformed_head": 1}


def test_site_and_filter_keeps_a_lowercase_replacement_mid_sentence():
    """The head rule is about sentence position, not about case alone: mid
    sentence a lowercase replacement for a capitalized span is ordinary."""
    text = "The talk went on, It seemed, forever and ever and ever."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "forever and ever and ever", "forever", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert dropped.total == 0 and len(cands) == 1


def test_site_and_filter_keeps_a_capitalized_replacement_at_a_sentence_start():
    text = "It is clear that we should go now."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "It is clear that we", "We", "cuts the expletive")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert dropped.total == 0 and len(cands) == 1


def test_site_and_filter_drops_malformed_shape_fragment():
    """A span of more than six words whose replacement keeps under 40% of
    them is a fragment of the span, not a rewrite of it."""
    text = "the porter walked slowly across the wide empty hall and stopped."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "walked slowly across the wide empty hall", "crossed",
          "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert cands == [] and dropped.reasons == {"malformed_shape": 1}


def test_site_and_filter_keeps_legitimate_tightening_above_the_shape_floor():
    """Conservative on purpose: an economy pass earns its keep by cutting
    words, so 40% retention (not 80%) is the floor."""
    text = "She wrote in order to make sure that everything was ready by dawn."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "in order to make sure that everything was ready",
          "to ensure everything was ready", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert dropped.total == 0 and len(cands) == 1


def test_site_and_filter_never_applies_the_shape_rule_to_a_short_span():
    text = "It was really quite obviously a mistake she regretted."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "really quite obviously", "clearly", "tighter")],
        text_of, closing_quotes="”\"", model="m", lens="economy")
    assert dropped.total == 0 and len(cands) == 1


def test_word_retention_is_a_multiset_overlap():
    assert fl.word_retention("a b c d", "a b") == 0.5
    assert fl.word_retention("a a b c", "a b c") == 0.75
    assert fl.word_retention("", "anything") == 1.0


def test_at_sentence_start_handles_an_opening_quote_after_the_boundary():
    text = 'She stopped. “It made me think,” she said.'
    assert fl.at_sentence_start(text, 0) is True
    assert fl.at_sentence_start(text, text.index("It made")) is True
    assert fl.at_sentence_start(text, text.index("made me")) is False


# --- editorial notes: strip when there is a replacement left, else drop ------

def test_site_and_filter_strips_a_parenthetical_editorial_note():
    """The shipped failure: the model addressed the EDITOR inside the
    replacement text, and the note went into the manuscript."""
    text = "She was an 18 year old girl from the coast."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "18 year old girl",
          "eighteen-year-old girl (spell out and hyphenate)", "house style")],
        text_of, closing_quotes="”\"", model="m", lens="word_choice")
    assert dropped.total == 0
    assert len(cands) == 1
    assert cands[0].replacement == "eighteen-year-old girl"
    assert dropped.stripped == 1


def test_site_and_filter_strips_a_dash_led_editorial_note():
    text = "He walked in, and then, sat down at the table."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "and then, sat", "and then sat — stray comma", "r")],
        text_of, closing_quotes="”\"", model="m", lens="clarity")
    assert dropped.total == 0
    assert cands[0].replacement == "and then sat"
    assert dropped.stripped == 1


def test_site_and_filter_drops_a_replacement_that_is_only_a_note():
    text = "He walked in and then sat down at the table."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "walked in and", "(missing comma)", "r")],
        text_of, closing_quotes="”\"", model="m", lens="clarity")
    assert cands == []
    assert dropped.reasons == {"editorial_note": 1}
    assert dropped.stripped == 0


def test_site_and_filter_leaves_an_ordinary_parenthetical_alone():
    text = "He walked in (as he always did) and then sat down at last."
    text_of = {"body-0001": text}
    cands, dropped = fl.site_and_filter(
        [("body-0001", "and then sat down at last",
          "and sat down (as he always did)", "r")],
        text_of, closing_quotes="”\"", model="m", lens="flow")
    assert dropped.total == 0
    assert cands[0].replacement == "and sat down (as he always did)"
    assert dropped.stripped == 0


def test_strip_editorial_note_round_trips_untouched_text_byte_identical():
    assert fl.strip_editorial_note("a plain replacement") == (
        "a plain replacement", False)


def test_load_external_proposals_merges_the_drop_reasons_across_tags():
    text_of = {"body-0001": "It was obviously a mistake she regretted."}
    rows = [
        {"para_id": "body-0001", "quote": "obviously", "replacement": "obviously",
         "model": "a", "lens": "economy"},
        {"para_id": "body-0001", "quote": "nowhere in the text",
         "replacement": "x", "model": "b", "lens": "flow"},
        {"para_id": "body-0001", "quote": "obviously",
         "replacement": "plainly (consider cutting)", "model": "b", "lens": "flow"},
    ]
    cands, dropped = fl.load_external_proposals(
        rows, text_of, closing_quotes="”\"")
    assert len(cands) == 1 and cands[0].replacement == "plainly"
    assert dropped.reasons == {"no_op": 1, "unanchored": 1}
    assert dropped.stripped == 1


# --- propose_flight: end-to-end over a FakeProvider --------------------------

def test_propose_flight_sites_and_tags_flight():
    paras = [_para("body-0001",
                   "It was really quite obviously a mistake she had made.")]
    prov = FakeProvider([_suggest(
        _prop(quote="really quite obviously", replacement="clearly"))])
    usage = Usage()
    cands, dropped, windows, failed = fl.propose_flight(
        paras, prov, model="gpt-5.6-luna", lens="economy", max_tokens=2000,
        usage=usage)
    assert dropped == 0 and windows == 1 and failed == 0
    assert len(cands) == 1
    assert cands[0].flight == "gpt-5.6-luna:economy"
    assert usage.api_calls == 1
    assert usage.by_model["gpt-5.6-luna"]["api_calls"] == 1


def test_propose_flight_counts_a_failed_window():
    paras = [_para("body-0001", "Some sentence that is long enough to review.")]
    prov = FakeProvider([ProviderResult(parsed=None, stop_reason="max_tokens")])
    usage = Usage()
    cands, dropped, windows, failed = fl.propose_flight(
        paras, prov, model="gpt-5.6-luna", lens="economy", max_tokens=2000,
        usage=usage)
    assert cands == [] and windows == 1 and failed == 1


def test_propose_flight_skips_non_reviewable_paragraphs():
    paras = [_para("body-0001", "Chapter One", reviewable=False),
            _para("body-0002", "An ordinary sentence in the narrative.")]
    prov = FakeProvider([_suggest()])
    usage = Usage()
    fl.propose_flight(paras, prov, model="gpt-5.6-luna", lens="economy",
                      max_tokens=2000, usage=usage)
    sent_body = prov.calls[0]["user"]
    assert "body-0002" in sent_body and "body-0001" not in sent_body


# --- load_external_proposals: a subagent flight without an API call ---------

def test_load_external_proposals_tags_rows_by_their_own_model_lens():
    text_of = {"body-0001": "It was really quite obviously a mistake."}
    rows = [{"para_id": "body-0001", "quote": "really quite obviously",
            "replacement": "clearly", "rationale": "tighter",
            "model": "subagent", "lens": "economy"}]
    cands, dropped = fl.load_external_proposals(
        rows, text_of, closing_quotes="”\"")
    assert dropped == 0
    assert cands[0].flight == "subagent:economy"


def test_load_external_proposals_defaults_when_row_has_no_tag():
    text_of = {"body-0001": "It was really quite obviously a mistake."}
    rows = [{"para_id": "body-0001", "original": "really quite obviously",
            "replacement": "clearly"}]
    cands, _dropped = fl.load_external_proposals(
        rows, text_of, closing_quotes="”\"", model="claude-code-session",
        lens="general")
    assert cands[0].flight == "claude-code-session:general"


def test_load_external_proposals_goes_through_the_same_filters():
    text_of = {"body-0001": "It was really quite obviously a mistake."}
    rows = [{"para_id": "body-0001", "quote": "not present", "replacement": "x"}]
    cands, dropped = fl.load_external_proposals(
        rows, text_of, closing_quotes="”\"")
    assert cands == [] and dropped == 1


# --- clustering: transitive merge, agreement counts distinct flights --------

def test_cluster_proposals_merges_transitive_overlap():
    text = "The quick brown fox jumped over the lazy dog in the yard."
    text_of = {"body-0001": text}
    # Three spans: A=[4,15) "quick brown", B=[10,21) overlaps A, C=[17,28)
    # overlaps B but not A -- a transitive chain, not a pairwise one.
    a = fl.Proposal(para_id="body-0001", start=4, end=15, original=text[4:15],
                    replacement="X", rationale="", model="m1", lens="economy")
    b = fl.Proposal(para_id="body-0001", start=10, end=21,
                    original=text[10:21], replacement="Y", rationale="",
                    model="m1", lens="flow")
    c = fl.Proposal(para_id="body-0001", start=17, end=28,
                    original=text[17:28], replacement="Z", rationale="",
                    model="m2", lens="economy")
    clusters = fl.cluster_proposals(
        {"m1:economy": [a], "m1:flow": [b], "m2:economy": [c]}, text_of)
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.start == 4 and cl.end == 28
    assert cl.agreement == 3          # 3 distinct flight keys
    assert set(cl.flights) == {"m1:economy", "m1:flow", "m2:economy"}


def test_cluster_proposals_keeps_non_overlapping_spans_separate():
    text = "One sentence here. Another sentence there, far away."
    text_of = {"body-0001": text}
    a = fl.Proposal(para_id="body-0001", start=0, end=3, original="One",
                    replacement="A", rationale="", model="m1", lens="economy")
    b = fl.Proposal(para_id="body-0001", start=40, end=44,
                    original=text[40:44], replacement="B", rationale="",
                    model="m1", lens="economy")
    clusters = fl.cluster_proposals({"m1:economy": [a, b]}, text_of)
    assert len(clusters) == 2


def test_cluster_agreement_counts_distinct_flights_not_raw_candidates():
    text = "A short clause here."
    text_of = {"body-0001": text}
    # Two candidates from the SAME flight, overlapping: one flight, not two.
    a = fl.Proposal(para_id="body-0001", start=0, end=1, original="A",
                    replacement="X", rationale="", model="m1", lens="economy")
    b = fl.Proposal(para_id="body-0001", start=0, end=5, original=text[0:5],
                    replacement="Y", rationale="", model="m1", lens="economy")
    clusters = fl.cluster_proposals({"m1:economy": [a, b]}, text_of)
    assert len(clusters) == 1
    assert clusters[0].agreement == 1


# --- judge: both postures select the right system prompt --------------------

def test_judge_clusters_strict_posture_sends_strict_system():
    cl = fl.Cluster(para_id="p", start=0, end=3, original="foo",
                    sentence="foo bar.", para_text="foo bar.",
                    options=[fl.Proposal(para_id="p", start=0, end=3,
                                         original="foo", replacement="baz",
                                         rationale="", model="m", lens="economy")])
    prov = FakeProvider([_verdict(verdict="keep")])
    usage = Usage()
    fl.judge_clusters([cl], prov, model="claude-fable-5", posture="strict",
                      usage=usage)
    assert prov.calls[0]["system"] == fl.STRICT_JUDGE_SYSTEM


def test_judge_clusters_lenient_posture_sends_lenient_system():
    cl = fl.Cluster(para_id="p", start=0, end=3, original="foo",
                    sentence="foo bar.", para_text="foo bar.",
                    options=[fl.Proposal(para_id="p", start=0, end=3,
                                         original="foo", replacement="baz",
                                         rationale="", model="m", lens="economy")])
    prov = FakeProvider([_verdict(verdict="keep")])
    usage = Usage()
    fl.judge_clusters([cl], prov, model="claude-fable-5", posture="lenient",
                      usage=usage)
    assert prov.calls[0]["system"] == fl.LENIENT_JUDGE_SYSTEM


def test_judge_clusters_unrecognised_posture_falls_back_to_strict():
    cl = fl.Cluster(para_id="p", start=0, end=3, original="foo",
                    sentence="foo bar.", para_text="foo bar.",
                    options=[fl.Proposal(para_id="p", start=0, end=3,
                                         original="foo", replacement="baz",
                                         rationale="", model="m", lens="economy")])
    prov = FakeProvider([_verdict(verdict="keep")])
    usage = Usage()
    fl.judge_clusters([cl], prov, model="claude-fable-5", posture="???",
                      usage=usage)
    assert prov.calls[0]["system"] == fl.STRICT_JUDGE_SYSTEM


def test_judge_cluster_returns_none_on_a_failed_call():
    cl = fl.Cluster(para_id="p", start=0, end=3, original="foo",
                    sentence="foo bar.", para_text="foo bar.",
                    options=[fl.Proposal(para_id="p", start=0, end=3,
                                         original="foo", replacement="baz",
                                         rationale="", model="m", lens="economy")])
    prov = FakeProvider([ProviderResult(parsed=None, stop_reason="max_tokens")])
    usage = Usage()
    verdicts = fl.judge_clusters([cl], prov, model="claude-fable-5",
                                 usage=usage)
    assert verdicts == [None]


# --- accept: the confidence floor --------------------------------------------

def _cluster(idx: int = 0) -> fl.Cluster:
    return fl.Cluster(para_id="p", start=idx, end=idx + 3, original="foo",
                      sentence="foo bar.", para_text="foo bar.",
                      options=[fl.Proposal(para_id="p", start=idx, end=idx + 3,
                                           original="foo", replacement="baz",
                                           rationale="", model="m", lens="economy")])


def test_accept_default_floor_keeps_medium_drops_low():
    clusters = [_cluster(0), _cluster(1)]
    verdicts = [
        fl._Verdict(verdict="pick", chosen_index=0, chosen_text="baz",
                   confidence="medium", reason=""),
        fl._Verdict(verdict="pick", chosen_index=0, chosen_text="baz",
                   confidence="low", reason=""),
    ]
    accepted, counts = fl.accept(clusters, verdicts, min_confidence="medium")
    assert len(accepted) == 1 and counts.accepted == 1
    assert counts.below_floor == 1


def test_accept_keep_verdict_is_never_accepted_regardless_of_confidence():
    clusters = [_cluster(0)]
    verdicts = [fl._Verdict(verdict="keep", chosen_index=-1, chosen_text="",
                           confidence="high", reason="")]
    accepted, counts = fl.accept(clusters, verdicts, min_confidence="low")
    assert accepted == [] and counts.kept == 1


def test_accept_counts_a_failed_judge_call_as_unjudged_not_kept():
    clusters = [_cluster(0)]
    accepted, counts = fl.accept(clusters, [None], min_confidence="low")
    assert accepted == [] and counts.unjudged == 1 and counts.kept == 0


def test_accept_low_floor_admits_a_low_confidence_pick():
    clusters = [_cluster(0)]
    verdicts = [fl._Verdict(verdict="pick", chosen_index=0, chosen_text="baz",
                           confidence="low", reason="")]
    accepted, counts = fl.accept(clusters, verdicts, min_confidence="low")
    assert len(accepted) == 1 and counts.accepted == 1


# --- findings: real edit-channel Findings, lane-tagged JSON ------------------

def test_findings_from_accepted_are_never_force_query():
    text = "It was really quite obviously a mistake she regretted."
    cl = fl.Cluster(para_id="body-0001", start=7, end=27,
                    original=text[7:27], sentence=text, para_text=text,
                    options=[fl.Proposal(para_id="body-0001", start=7, end=27,
                                         original=text[7:27],
                                         replacement="clearly",
                                         rationale="cuts the pile-up",
                                         model="gpt-5.6-luna", lens="economy")])
    verdict = fl._Verdict(verdict="pick", chosen_index=0, chosen_text="clearly",
                          confidence="high", reason="tighter")
    findings = fl.findings_from_accepted([(cl, verdict)], itertools.count(1))
    assert len(findings) == 1
    f = findings[0]
    assert f.force_query is False
    assert f.error_type == fl.LANE == "copyedit"
    assert f.confidence == "high"
    assert "clearly" in f.corrected_text
    assert f.agreement == 1


def test_finding_to_json_carries_the_lane_key():
    text = "It was really quite obviously a mistake she regretted."
    cl = fl.Cluster(para_id="body-0001", start=7, end=27,
                    original=text[7:27], sentence=text, para_text=text,
                    options=[fl.Proposal(para_id="body-0001", start=7, end=27,
                                         original=text[7:27],
                                         replacement="clearly", rationale="",
                                         model="gpt-5.6-luna", lens="economy")])
    verdict = fl._Verdict(verdict="pick", chosen_index=0, chosen_text="clearly",
                          confidence="high", reason="")
    findings = fl.findings_from_accepted([(cl, verdict)], itertools.count(1))
    row = fl.finding_to_json(findings[0])
    assert row["lane"] == "copyedit"
    assert row["error_type"] == "copyedit"
    assert row["force_query"] is False
    # Round-trips through json.dumps cleanly (a merge-desk / galley-score
    # consumer reads this file straight off disk).
    json.dumps(row)


# --- fact changes are demoted to a QUERY, whatever the judge said -----------

def _fact_cluster(text: str, span: str) -> fl.Cluster:
    start = text.index(span)
    end = start + len(span)
    return fl.Cluster(
        para_id="body-0001", start=start, end=end, original=span,
        sentence=text, para_text=text,
        options=[fl.Proposal(para_id="body-0001", start=start, end=end,
                             original=span, replacement="(unused)",
                             rationale="reads better", model="gpt-5.6-luna",
                             lens="word_choice")])


def _one_finding(text: str, span: str, chosen: str):
    cl = _fact_cluster(text, span)
    v = fl._Verdict(verdict="pick", chosen_index=0, chosen_text=chosen,
                    confidence="high", reason="reads better")
    findings = fl.findings_from_accepted([(cl, v)], itertools.count(1))
    assert len(findings) == 1
    return findings[0]


def test_fact_change_flags_an_altered_number():
    assert fl.fact_change("one 12 minute talk",
                          "one twenty-one-minute talk") == 'number "12" removed'
    assert fl.fact_change("12 people", "21 people") == 'number "12" -> "21"'
    assert fl.fact_change("a few people", "3 people") == 'number "3" added'


def test_fact_change_flags_a_new_proper_noun():
    assert fl.fact_change("False Expectations Appearing Real",
                          "False Evidence Appearing Real") == (
        'proper noun "Evidence" not in the original')
    assert fl.fact_change("my friend\'s luck", "Ava\'s luck") == (
        'proper noun "Ava" not in the original')


def test_fact_change_ignores_a_word_that_is_merely_re_cased():
    assert fl.fact_change("the general said so", "the General said so") is None
    assert fl.fact_change("we should go", "We should go") is None


def test_fact_change_ignores_a_forced_capital_at_a_sentence_opening():
    """Recasting a sentence opening capitalizes whatever word lands first —
    that capital is orthography, not a name, and must not read as a fact
    change."""
    assert fl.fact_change("It is clear that we should go",
                          "Clearly, we should go") is None


def test_fact_change_flags_a_rewritten_quoted_or_italic_title():
    assert fl.fact_change('he reread “The Odyssey” again',
                          'he reread “The Iliad” again') == (
        'title "The Odyssey" -> "The Iliad"')
    assert fl.fact_change("she opened _Middlemarch_ once more",
                          "she opened _Bleak House_ once more") == (
        'title "Middlemarch" -> "Bleak House"')


def test_fact_change_passes_ordinary_taste_edits_through():
    assert fl.fact_change("very unique", "unique") is None
    assert fl.fact_change("in order to", "to") is None
    assert fl.fact_change("really quite obviously", "clearly") is None
    assert fl.fact_change("he walked slowly", "he ambled") is None


def test_accepted_number_change_is_demoted_to_a_query():
    text = "He gave one 12 minute talk that year and then went home."
    f = _one_finding(text, "one 12 minute talk", "one twenty-one-minute talk")
    assert f.force_query is True
    assert f.corrected_text == f.original_text      # a question changes nothing
    assert "12" in f.explanation and "twenty-one-minute" in f.explanation
    assert f.error_type == fl.LANE


def test_accepted_backronym_change_is_demoted_to_a_query():
    text = "FEAR stands for False Expectations Appearing Real, she told them."
    f = _one_finding(text, "False Expectations Appearing Real",
                     "False Evidence Appearing Real")
    assert f.force_query is True
    assert f.corrected_text == f.original_text
    assert "Evidence" in f.explanation


def test_accepted_new_proper_noun_is_demoted_to_a_query():
    text = "It was my friend\'s luck that carried them both through the night."
    f = _one_finding(text, "my friend\'s luck", "Ava\'s luck")
    assert f.force_query is True
    assert f.corrected_text == f.original_text
    assert "Ava" in f.explanation


def test_a_demoted_finding_still_serializes_as_a_lane_row():
    text = "He gave one 12 minute talk that year and then went home."
    f = _one_finding(text, "one 12 minute talk", "one twenty-one-minute talk")
    row = fl.finding_to_json(f)
    assert row["lane"] == "copyedit" and row["force_query"] is True
    json.dumps(row)


def test_ordinary_taste_edits_are_not_demoted():
    text = "It was a very unique idea, and in order to test it she waited."
    for span, chosen in (("very unique", "unique"), ("in order to", "to")):
        f = _one_finding(text, span, chosen)
        assert f.force_query is False
        assert f.corrected_text != f.original_text
        assert chosen in f.corrected_text


# --- cluster JSON round-trips self-contained (para_text travels with it) ----

def test_cluster_json_round_trip_needs_no_source_manuscript():
    text = "It was really quite obviously a mistake."
    cl = fl.Cluster(para_id="body-0001", start=7, end=27, original=text[7:27],
                    sentence=text, para_text=text,
                    options=[fl.Proposal(para_id="body-0001", start=7, end=27,
                                         original=text[7:27],
                                         replacement="clearly", rationale="r",
                                         model="gpt-5.6-luna", lens="economy")])
    restored = fl.Cluster.from_json(json.loads(json.dumps(cl.to_json())))
    assert restored.para_text == text
    assert restored.options[0].flight == "gpt-5.6-luna:economy"


# --- cost projection ---------------------------------------------------------

def test_project_cost_shape_and_scale():
    flights = fl.flight_matrix(["gpt-5.6-luna"])
    proj = fl.project_cost(2000, flights, "claude-fable-5")
    assert proj["words"] == 2000
    assert len(proj["flights"]) == len(fl.LENSES) == 6
    assert proj["est_clusters"] >= 1
    assert proj["total_usd"] > 0
    assert proj["judge_model"] == "claude-fable-5"


def test_flight_matrix_is_models_outer_lenses_inner_and_deterministic():
    specs = fl.flight_matrix(["a", "b"], ["economy", "flow"])
    assert [s.key for s in specs] == ["a:economy", "a:flow", "b:economy",
                                      "b:flow"]


# --- CLI: `docproof galley flights` ------------------------------------------
#
# The CLI is thin glue over the module above, so these test the wiring: the
# provider seam, the three modes (full pipeline / --propose-only /
# --judge-only), and the findings envelope actually written to disk. No test
# here makes a network call — `_flights_provider_of` is monkeypatched to hand
# back one FakeProvider for every model id, the same seam
# tests/test_smoothing.py patches `build_provider` through.

from docproof.__main__ import main  # noqa: E402
from .conftest import FIXTURES  # noqa: E402

BODY0 = "The manuscript was finished, nobody wanted to read it."


def _patch_provider(monkeypatch, prov):
    import docproof.__main__ as m
    monkeypatch.setattr(m, "_flights_provider_of", lambda cfg: (lambda model: prov))


def test_flights_cli_dry_run_makes_no_provider_call(tmp_path, monkeypatch, capsys):
    def _boom(cfg):
        raise AssertionError("dry-run must not construct a provider")
    import docproof.__main__ as m
    monkeypatch.setattr(m, "_flights_provider_of", _boom)

    rc = main(["galley", "flights", str(FIXTURES / "simple.docx"), "--dry-run",
              "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROJECTED total" in out
    assert not (tmp_path / "flights_findings.json").exists()


def test_flights_cli_requires_input_unless_judge_only(tmp_path, capsys):
    rc = main(["galley", "flights", "--out", str(tmp_path)])
    assert rc == 2
    assert "an input manuscript is required" in capsys.readouterr().err


def test_flights_cli_full_pipeline_writes_lane_tagged_findings(tmp_path,
                                                               monkeypatch):
    prov = FakeProvider([
        _suggest(_prop(para_id="body-0000",
                      quote="nobody wanted to read it",
                      replacement="no one wanted to read it",
                      rationale="more natural")),
        _verdict(verdict="pick", chosen_index=0,
                chosen_text="no one wanted to read it", confidence="high"),
    ])
    _patch_provider(monkeypatch, prov)

    rc = main(["galley", "flights", str(FIXTURES / "simple.docx"),
              "--lenses", "economy", "--models", "gpt-5.6-luna",
              "--out", str(tmp_path)])
    assert rc == 0

    payload = json.loads((tmp_path / "flights_findings.json").read_text())
    assert payload["lane"] == "copyedit"
    # The house default posture: the copy-edit lane offers, the author decides.
    assert payload["posture"] == "lenient"
    assert payload["checkpoint"] is None
    assert "total_usd" in payload["cost"]
    assert len(payload["findings"]) == 1
    f = payload["findings"][0]
    assert f["lane"] == "copyedit"
    assert f["error_type"] == "copyedit"
    assert f["force_query"] is False
    assert "no one wanted to read it" in f["corrected_text"]


def test_flights_cli_propose_only_then_judge_only_round_trip(tmp_path,
                                                             monkeypatch):
    propose_prov = FakeProvider([
        _suggest(_prop(para_id="body-0000",
                      quote="nobody wanted to read it",
                      replacement="no one wanted to read it",
                      rationale="more natural")),
    ])
    _patch_provider(monkeypatch, propose_prov)

    rc = main(["galley", "flights", str(FIXTURES / "simple.docx"),
              "--lenses", "economy", "--models", "gpt-5.6-luna",
              "--propose-only", "--out", str(tmp_path)])
    assert rc == 0
    clusters_path = tmp_path / "clusters.json"
    assert clusters_path.exists()
    assert not (tmp_path / "flights_findings.json").exists()
    clusters = json.loads(clusters_path.read_text())
    assert len(clusters["clusters"]) == 1
    assert clusters["clusters"][0]["para_text"]        # self-contained

    judge_prov = FakeProvider([
        _verdict(verdict="pick", chosen_index=0,
                chosen_text="no one wanted to read it", confidence="high"),
    ])
    _patch_provider(monkeypatch, judge_prov)

    rc = main(["galley", "flights", "--judge-only", str(clusters_path),
              "--out", str(tmp_path)])
    assert rc == 0
    payload = json.loads((tmp_path / "flights_findings.json").read_text())
    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["lane"] == "copyedit"


def test_flights_cli_external_proposals_merge_before_clustering(tmp_path,
                                                                monkeypatch):
    ext_path = tmp_path / "external.json"
    ext_path.write_text(json.dumps({"proposals": [
        {"para_id": "body-0000", "quote": "nobody wanted to read it",
         "replacement": "no one wanted to read it", "rationale": "subagent",
         "model": "claude-session", "lens": "word_choice"},
    ]}), encoding="utf-8")

    prov = FakeProvider([
        _suggest(),  # the API flight itself proposes nothing this run
        _verdict(verdict="pick", chosen_index=0,
                chosen_text="no one wanted to read it", confidence="high"),
    ])
    _patch_provider(monkeypatch, prov)

    rc = main(["galley", "flights", str(FIXTURES / "simple.docx"),
              "--lenses", "economy", "--models", "gpt-5.6-luna",
              "--external-proposals", str(ext_path), "--out", str(tmp_path)])
    assert rc == 0
    payload = json.loads((tmp_path / "flights_findings.json").read_text())
    assert len(payload["findings"]) == 1
    assert "no one wanted to read it" in payload["findings"][0]["corrected_text"]
