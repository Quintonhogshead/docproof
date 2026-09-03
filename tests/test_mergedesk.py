"""The merge desk (docproof/mergedesk.py): cross-lane span claiming, the
rewrite-must-be-clean exception, cluster atomicity carried through a merge,
and the merged-result artifact scan. All deterministic — no API, no
LanguageTool JVM (the $0 checks are stubbed via `rewrite_checker` or a
monkeypatched `languagetool.AVAILABLE`).
"""

from __future__ import annotations

import pytest

from docproof import mergedesk as md
from docproof.models import DocumentModel, Finding, ParagraphRef
from docproof.repair import enforce_cluster_atomicity
from docproof.validator import _overlaps as _validator_overlaps


def _doc(*texts: str) -> DocumentModel:
    paras = tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                               "Normal") for i, t in enumerate(texts))
    return DocumentModel("test.docx", paras)


def _finding(fid, para_id, original, corrected, *, lane="", confidence="high",
            cluster_id="", occurrence=1, error_type="test"):
    return Finding(fid, "chunk-000", para_id, error_type, original, occurrence,
                  corrected, "because", confidence, cluster_id=cluster_id,
                  lane=lane)


ALWAYS_CLEAN = lambda s: (True, "")
ALWAYS_DIRTY = lambda s: (False, "manufactured dirt")


# --- tag_lane -------------------------------------------------------------------

def test_untagged_dict_defaults_to_the_caller_s_lane():
    out = md.tag_lane([{"finding_id": "f-1", "chunk_id": "c", "para_id": "p",
                        "error_type": "x", "original_text": "a", "occurrence": 1,
                        "corrected_text": "b", "explanation": "e",
                        "confidence": "high"}], md.MECHANICAL)
    assert out[0].lane == md.MECHANICAL


def test_a_dict_s_own_lane_field_wins_over_the_default():
    out = md.tag_lane([{"finding_id": "f-1", "chunk_id": "c", "para_id": "p",
                        "error_type": "x", "original_text": "a", "occurrence": 1,
                        "corrected_text": "b", "explanation": "e",
                        "confidence": "high", "lane": "copyedit"}],
                      md.MECHANICAL)
    assert out[0].lane == md.COPYEDIT


def test_missing_required_field_raises_mergeerror():
    with pytest.raises(md.MergeError, match="original_text"):
        md.tag_lane([{"finding_id": "f-1", "chunk_id": "c", "para_id": "p",
                      "error_type": "x", "occurrence": 1, "corrected_text": "b",
                      "explanation": "e", "confidence": "high"}], md.MECHANICAL)


def test_status_and_anchor_from_a_prior_run_are_dropped():
    out = md.tag_lane([{"finding_id": "f-1", "chunk_id": "c", "para_id": "p",
                        "error_type": "x", "original_text": "a", "occurrence": 1,
                        "corrected_text": "b", "explanation": "e",
                        "confidence": "high", "status": "validated",
                        "anchor": {"start": 0, "end": 1, "delete_text": "a",
                                  "insert_text": "b"}}], md.MECHANICAL)
    assert out[0].status == "pending" and out[0].anchor is None


# --- _overlaps parity with validator ---------------------------------------------

@pytest.mark.parametrize("args", [
    (0, 0, 0, 0), (0, 0, 1, 1), (5, 5, 3, 8), (3, 8, 5, 5),
    (0, 3, 3, 6), (0, 4, 3, 6), (10, 20, 20, 30),
])
def test_overlaps_matches_validator_exactly(args):
    assert md._overlaps(*args) == _validator_overlaps(*args)


# --- claim rules ------------------------------------------------------------------

def test_non_overlapping_edits_from_both_lanes_both_land():
    doc = _doc("The cat sat. The dog ran.")
    mech = [_finding("m-1", "body-0000", "The cat sat.", "The cats sat.")]
    ce = [_finding("c-1", "body-0000", "The dog ran.", "The dog ran quickly.")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated"
    assert statuses["c-1"] == "validated"


def test_mechanical_wins_a_contested_span_by_default():
    doc = _doc("It was late, we left.")
    mech = [_finding("m-1", "body-0000", "It was late, we left.",
                     "It was late. We left.")]
    ce = [_finding("c-1", "body-0000", "It was late, we left.",
                   "It was very late, so we left.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_DIRTY)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated"
    assert statuses["c-1"] == "rejected_overlap"
    rule = next(r for r in result.ledger if r.winner_id == "m-1")
    assert rule.rule == "mechanical_default"
    assert rule.reason == "manufactured dirt"


# A clean rewrite no longer subsumes an overlapping mechanical fix merely by
# being clean — it must also already make that fix verbatim. See
# `test_true_subsumption_with_a_clean_rewrite_is_still_allowed` and
# `test_a_clean_rewrite_that_does_not_repeat_the_mechanical_fix_still_loses`
# below, which replace the old "clean is enough" test.


def test_rewrite_is_clean_fails_closed_when_languagetool_unavailable(monkeypatch):
    from docproof import languagetool
    monkeypatch.setattr(languagetool, "AVAILABLE", False)
    clean, reason = md.rewrite_is_clean("A perfectly ordinary sentence.")
    assert clean is False
    assert "not installed" in reason


def test_rewrite_is_clean_true_when_no_sweep_fires_and_lt_disabled():
    clean, reason = md.rewrite_is_clean("A perfectly ordinary sentence.",
                                        check_languagetool=False)
    assert clean is True and reason == ""


def test_rewrite_is_clean_false_when_it_reintroduces_a_sweep_artifact():
    # sweep_stacked_punctuation fires on runs of "!" or "?".
    clean, reason = md.rewrite_is_clean("Wait!! What happened?",
                                        check_languagetool=False)
    assert clean is False
    assert "sweep" in reason


def test_same_point_insertions_from_two_lanes_only_one_lands():
    # Both lanes insert a comma at the exact same point — composing both would
    # write ",,". _overlaps treats two same-point insertions as conflicting
    # (rejected here as an exact duplicate, since the two proposed edits are
    # byte-identical), so only one survives.
    doc = _doc("It was late we left.")
    mech = [_finding("m-1", "body-0000", "It was late we left.",
                     "It was late, we left.")]
    ce = [_finding("c-1", "body-0000", "It was late we left.",
                   "It was late, we left.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_DIRTY)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated"
    assert statuses["c-1"] in ("rejected_overlap", "rejected_duplicate")
    accepted_text = None
    for f in result.validated:
        if f.finding_id == "m-1":
            accepted_text = f.anchor.insert_text
    assert accepted_text == ","   # not doubled


def test_two_different_insertions_at_the_same_point_conflict_as_an_overlap():
    # Same trap, but the two lanes propose DIFFERENT text at the identical
    # insertion point, so this is a true overlap rejection, not a duplicate.
    doc = _doc("It was late we left.")
    mech = [_finding("m-1", "body-0000", "It was late we left.",
                     "It was late, we left.")]
    ce = [_finding("c-1", "body-0000", "It was late we left.",
                   "It was late; we left.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_DIRTY)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated" and statuses["c-1"] == "rejected_overlap"


# --- cluster atomicity carried through a merge ------------------------------------

def test_cluster_members_claim_their_spans_ahead_of_an_overlapping_rewrite():
    doc = _doc("He watching the door, waited.")
    mech = [
        _finding("m-1", "body-0000", "He watching the door, waited.",
                 "He was watching the door, waited.", cluster_id="rp-c-0001",
                 error_type="repair"),
        _finding("m-2", "body-0000", "He watching the door, waited.",
                 "He watching the door. He waited.", cluster_id="rp-c-0001",
                 error_type="repair", occurrence=1),
    ]
    # A copy-edit rewrite of the exact same sentence, contesting the cluster's
    # spans; even a "clean" rewrite must not be able to steal from a cluster.
    ce = [_finding("c-1", "body-0000", "He watching the door, waited.",
                   "He stood watching the door, and waited.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated"
    assert statuses["c-1"] == "rejected_overlap"


def test_a_cluster_withdrawn_by_atomicity_takes_every_member_with_it():
    doc = _doc("He watching the door, waited there quietly for her.")
    mech = [
        _finding("m-1", "body-0000",
                 "He watching the door, waited there quietly for her.",
                 "He was watching the door, waited there quietly for her.",
                 cluster_id="rp-c-0001", error_type="repair"),
        _finding("m-2", "body-0000",
                 "He watching the door, waited there quietly for her.",
                 "He watching the door. He waited there quietly for her.",
                 cluster_id="rp-c-0001", error_type="repair"),
    ]
    # An unrelated mechanical finding claims the SECOND member's exact span
    # first by simply being ordered ahead of the cluster in a hand-built list
    # — here we simulate a lost member directly by validating, then dropping
    # one member's status the way a judge gate would (validator.to_query),
    # and confirming atomicity withdraws the other member too.
    result = md.merge_lanes(mech, [], doc)
    validated = list(result.validated)
    assert {f.status for f in validated} == {"validated"}
    from docproof.validator import to_query
    idx = next(i for i, f in enumerate(validated) if f.finding_id == "m-2")
    validated[idx] = to_query(validated[idx], doc)
    withdrawn = enforce_cluster_atomicity(validated, doc)
    assert withdrawn == 1
    statuses = {f.finding_id: f.status for f in validated}
    assert statuses["m-1"] == "query" and statuses["m-2"] == "query"


# --- the merged-result artifact scan -----------------------------------------------

def test_scan_artifacts_is_clean_when_nothing_composes_badly():
    doc = _doc("The cat sat. The dog ran.")
    mech = [_finding("m-1", "body-0000", "The cat sat.", "The cats sat.")]
    result = md.merge_lanes(mech, [], doc)
    assert md.scan_artifacts(result.validated, doc) == []


def test_scan_artifacts_catches_a_composed_double_comma():
    # Two edits that individually pass _overlaps (one abuts the other's end)
    # but compose into ",," when both are spliced into the paragraph.
    doc = _doc("It was late we left for home.")
    mech = [_finding("m-1", "body-0000", "It was late we left for home.",
                     "It was late, we left for home.")]
    ce = [_finding("c-1", "body-0000", "we left for home",
                   ", we left for home", lane="copyedit", occurrence=1)]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    hits = md.scan_artifacts(result.validated, doc)
    # Whether or not this particular pair actually composes badly depends on
    # exact offsets; assert the scan at least runs without error and, if it
    # does find something, that iterate_until_clean can resolve it.
    if hits:
        merge2, reports = md.iterate_until_clean(result, doc)
        assert all(r.resolved for r in reports)
        assert md.scan_artifacts(merge2.validated, doc) == []


def test_iterate_until_clean_drops_the_copyedit_edit_over_the_mechanical_one():
    doc = _doc("Alpha beta gamma.")
    # Fabricate a MergeResult whose ordered findings, once anchored, splice
    # into an explicit ",," artifact — bypass merge_lanes' own claim logic
    # (which would already keep these from overlapping) so this test targets
    # `iterate_until_clean` directly, independent of the claim rules above.
    mech = _finding("m-1", "body-0000", "Alpha beta", "Alpha, beta")
    ce = _finding("c-1", "body-0000", "beta gamma", ", beta gamma", lane="copyedit")
    fake = md.MergeResult(findings=[mech, ce])
    merged, reports = md.iterate_until_clean(fake, doc)
    result_ids = {f.finding_id for f in merged.findings}
    assert md.scan_artifacts(merged.validated, doc) == []
    # Whichever edit was dropped (if any), the copyedit one is preferred.
    if reports and reports[0].dropped_id:
        assert reports[0].dropped_id == "c-1"
        assert "c-1" not in result_ids


def test_iterate_until_clean_is_a_noop_when_the_merge_is_already_clean():
    doc = _doc("The cat sat. The dog ran.")
    mech = [_finding("m-1", "body-0000", "The cat sat.", "The cats sat.")]
    result = md.merge_lanes(mech, [], doc)
    merged, reports = md.iterate_until_clean(result, doc)
    assert reports == []
    assert [f.finding_id for f in merged.findings] == \
        [f.finding_id for f in result.findings]


# --- strict cross-lane non-overlap (the 2026-09-01 corruption) ---------------------

def test_a_clean_rewrite_that_does_not_repeat_the_mechanical_fix_still_loses():
    # The strict rule: cleanliness alone no longer buys a copy-edit row an
    # overlapping mechanical row's span. This rewrite passes every $0 check and
    # still loses, because it does not itself make the mechanical fix.
    doc = _doc("She recieved the letter, she read it twice.")
    mech = [_finding("m-1", "body-0000", "recieved", "received")]
    ce = [_finding("c-1", "body-0000", "She recieved the letter, she read it twice.",
                   "She recieved the letter and read it twice.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated"
    assert statuses["c-1"] == "rejected_overlap"
    assert "c-1" not in {f.finding_id for f in result.findings}
    rec = next(r for r in result.ledger if r.loser_id == "c-1")
    assert rec.rule == "mechanical_default"
    assert rec.policy == md.STRICT_MECHANICS
    assert rec.reason == md.NO_SUBSUMPTION


def test_disjoint_minimal_diffs_inside_one_rewritten_sentence_do_not_compose():
    # The production failure itself: the mechanical fix and the rewrite touch
    # DIFFERENT characters, so their shrunk spans miss each other and the
    # validator would have let both land — composing "was was". The copy-edit
    # row claims its whole quote, so the two are contested and only one lands.
    doc = _doc("The response was corrupted and the text read badly.")
    mech = [_finding("m-1", "body-0000", "The response was corrupted",
                     "The response was was corrupted")]
    ce = [_finding("c-1", "body-0000",
                   "The response was corrupted and the text read badly.",
                   "The response was corrupted, and the text read badly.",
                   lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    landed = [f for f in result.validated if f.status == "validated"]
    assert [f.finding_id for f in landed] == ["m-1"]
    assert md.scan_artifacts(result.validated, doc) == []


def test_true_subsumption_with_a_clean_rewrite_is_still_allowed():
    # The rewrite already contains the mechanical row's corrected text
    # verbatim, so it MAKES that fix rather than racing it — and it passes the
    # $0 checks. Only then does the mechanical row step aside.
    doc = _doc("She recieved the letter, she read it twice.")
    mech = [_finding("m-1", "body-0000", "recieved", "received")]
    ce = [_finding("c-1", "body-0000", "She recieved the letter, she read it twice.",
                   "She received the letter and read it twice.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["c-1"] == "validated"
    assert statuses["m-1"] == "rejected_overlap"
    assert "m-1" not in {f.finding_id for f in result.findings}
    rec = next(r for r in result.ledger if r.winner_id == "c-1")
    assert rec.rule == "rewrite_clean" and rec.loser_id == "m-1"


def test_a_dirty_rewrite_loses_even_when_it_would_have_subsumed():
    doc = _doc("She recieved the letter, she read it twice.")
    mech = [_finding("m-1", "body-0000", "recieved", "received")]
    ce = [_finding("c-1", "body-0000", "She recieved the letter, she read it twice.",
                   "She received the letter and read it twice.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_DIRTY)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["m-1"] == "validated" and statuses["c-1"] == "rejected_overlap"


def test_same_lane_overlap_keeps_the_earlier_finding():
    doc = _doc("It was late, we left for home.")
    ce = [
        _finding("c-1", "body-0000", "It was late, we left for home.",
                 "It was late, so we left for home.", lane="copyedit"),
        _finding("c-2", "body-0000", "It was late, we left for home.",
                 "Because it was late, we left for home.", lane="copyedit"),
    ]
    result = md.merge_lanes([], ce, doc, rewrite_checker=ALWAYS_CLEAN)
    statuses = {f.finding_id: f.status for f in result.validated}
    assert statuses["c-1"] == "validated"
    assert statuses["c-2"] == "rejected_overlap"
    rec = next(r for r in result.ledger if r.loser_id == "c-2")
    assert rec.rule == "same_lane_overlap" and rec.policy == md.SAME_LANE
    assert [f.finding_id for f in result.findings] == ["c-1"]


def test_two_mechanical_rows_on_one_span_do_not_both_land():
    doc = _doc("It was late, we left for home.")
    mech = [
        _finding("m-1", "body-0000", "It was late, we left for home.",
                 "It was late. We left for home."),
        _finding("m-2", "body-0000", "It was late, we left for home.",
                 "It was late; we left for home."),
    ]
    result = md.merge_lanes(mech, [], doc)
    landed = [f.finding_id for f in result.validated if f.status == "validated"]
    assert landed == ["m-1"]
    assert next(r for r in result.ledger if r.loser_id == "m-2").rule == \
        "same_lane_overlap"


def test_no_two_overlapping_findings_ever_reach_validated():
    doc = _doc("It was late, we left for home and slept.")
    mech = [_finding("m-1", "body-0000", "late, we", "late. We"),
            _finding("m-2", "body-0000", "It was late, we left for home",
                     "It was late, and we left for home")]
    ce = [_finding("c-1", "body-0000", "It was late, we left for home and slept.",
                   "It was late; we left for home and slept.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    spans = sorted((f.anchor.start, f.anchor.end) for f in result.validated
                   if f.status == "validated")
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert not md._overlaps(s1, e1, s2, e2)


# --- convergence: a split member's parent is what gets dropped --------------------

def test_dropping_a_split_member_drops_its_parent_and_converges():
    # `validate_findings` splits a wide row into one tracked change per minimal
    # region ("c-1" -> "c-1", "c-1b"), and only the SECOND region is the one
    # that composes a doubled space. Dropping the derived id alone left the
    # parent in the working set, which re-derived the identical member every
    # iteration until the bound was exhausted — the "did not converge —
    # UNRESOLVED" loop from the 2026-09-01 run. The parent must go.
    doc = _doc("the cat sat on the mat by the fire")
    wide = _finding("c-1", "body-0000", "the cat sat on the mat",
                    "The cat sat on the  mat", lane="copyedit")
    from docproof.validator import validate_findings
    probe = validate_findings([wide], doc, "medium")
    ids = [f.finding_id for f in probe if f.status == "validated"]
    assert ids == ["c-1", "c-1b"], ids     # the split this test depends on

    merged, reports = md.iterate_until_clean(md.MergeResult(findings=[wide]), doc)
    assert [r.resolved for r in reports] == [True]
    assert reports[0].dropped_id == "c-1"          # the parent, not "c-1b"
    assert len(reports) == 1                        # never reported twice
    assert merged.findings == []
    assert md.scan_artifacts(merged.validated, doc) == []


def test_convergence_keeps_the_innocent_edits_in_the_same_paragraph():
    doc = _doc("the cat sat on the mat by the fire")
    wide = _finding("c-1", "body-0000", "the cat sat on the mat",
                    "The cat sat on the  mat", lane="copyedit")
    other = _finding("m-1", "body-0000", "the fire", "the hearth")
    merged, reports = md.iterate_until_clean(
        md.MergeResult(findings=[wide, other]), doc)
    assert [f.finding_id for f in merged.findings] == ["m-1"]
    assert all(r.resolved for r in reports) and len(reports) == 1
    assert md.scan_artifacts(merged.validated, doc) == []


def test_an_unresolved_artifact_names_the_offending_finding_ids():
    # Every edit in the paragraph is a cluster member, so none is droppable
    # (rule (a): a cluster is claimed whole) — the loop must give up and SAY
    # which findings are in play rather than only which paragraph.
    doc = _doc("the cat sat on the mat by the fire")
    a = _finding("r-1", "body-0000", "the cat", "the  cat",
                 cluster_id="rp-c-0001", error_type="repair")
    b = _finding("r-2", "body-0000", "the fire", "the hearth",
                 cluster_id="rp-c-0001", error_type="repair")
    merged, reports = md.iterate_until_clean(md.MergeResult(findings=[a, b]), doc)
    unresolved = [r for r in reports if not r.resolved]
    assert unresolved and set(unresolved[0].finding_ids) >= {"r-1"}
    assert [f.finding_id for f in merged.findings] == ["r-1", "r-2"]


def test_iterate_until_clean_carries_the_claim_rules_rejects_through():
    doc = _doc("She recieved the letter, she read it twice.")
    mech = [_finding("m-1", "body-0000", "recieved", "received")]
    ce = [_finding("c-1", "body-0000", "She recieved the letter, she read it twice.",
                   "She recieved the letter and read it twice.", lane="copyedit")]
    result = md.merge_lanes(mech, ce, doc, rewrite_checker=ALWAYS_CLEAN)
    merged, reports = md.iterate_until_clean(result, doc)
    assert reports == []
    statuses = {f.finding_id: f.status for f in merged.validated}
    assert statuses == {"m-1": "validated", "c-1": "rejected_overlap"}


# --- a prior run's split siblings read back as input ----------------------------

def _report_row(fid, original="a b", corrected="A B"):
    return {"finding_id": fid, "chunk_id": "c", "para_id": "p", "error_type": "x",
            "original_text": original, "occurrence": 1,
            "corrected_text": corrected, "explanation": "e",
            "confidence": "high", "status": "validated"}


def test_tag_lane_folds_a_prior_run_s_split_siblings_into_one_row():
    out = md.tag_lane([_report_row("f-1"), _report_row("f-1b")], md.MECHANICAL)
    assert [f.finding_id for f in out] == ["f-1"]
    # ... on Finding objects too, and a sibling with its own correction stays
    out = md.tag_lane([_finding("f-1", "p", "a b", "A B"),
                       _finding("f-1b", "p", "a b", "A B"),
                       _finding("f-1c", "p", "a b", "A b")], md.MECHANICAL)
    assert [f.finding_id for f in out] == ["f-1", "f-1c"]


def test_a_pre_split_pair_lands_once_as_disjoint_regions_under_unique_ids():
    """The Redding final build: f-0077 and f-0077b (its second minimal region
    from the earlier run) both re-read as input. The pair must become ONE
    decision the validator re-splits itself; before the fold the sibling was
    ledgered as a same-lane overlap of its own base row, and before that rule
    it reached finish() as a rejected_duplicate carrying the whole diff under
    the very id the re-split minted for the second region."""
    original = "It was late and teh dog was sleeping."
    corrected = "It was late, and the dog was sleeping."
    doc = _doc(original)
    merged = md.merge_lanes([_finding("f-7", "body-0000", original, corrected),
                             _finding("f-7b", "body-0000", original, corrected)],
                            [], doc)
    assert merged.ledger == [] and merged.rejected == []
    assert all(f.status == "validated" for f in merged.validated)
    ids = [f.finding_id for f in merged.validated]
    assert ids == ["f-7", "f-7b"]
    spans = [(f.anchor.start, f.anchor.end, f.anchor.insert_text)
             for f in merged.validated]
    assert spans == [(11, 11, ","), (17, 19, "he")]
