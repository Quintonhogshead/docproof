"""The finished-text delivery gates (P0-3, Purpura beta run 3).

certify reads no text for meaning; these two gates do. The deterministic parts —
locating applied edits, pairing each to its finished context, chunking, parsing,
rejecting a hallucinated id — are exercised here against a scripted fake
provider, so nothing requires an API key. The certify hooks that read the two
artifacts are covered too.
"""
from __future__ import annotations

import json

from docproof.models import Usage
from docproof.providers.base import ProviderResult
from galley import verify
from galley.manifest import _certify_change_verify, _certify_finished_walk


class _Provider:
    """Replays one scripted parsed body per call, records the calls."""

    def __init__(self, *bodies):
        self._bodies = list(bodies)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        body = self._bodies.pop(0) if self._bodies else {}
        return ProviderResult(parsed=body, stop_reason="ok")


# --- deterministic input selection --------------------------------------------

def test_applied_edits_keeps_only_landed_tracked_changes(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [
        {"para_id": "p1", "original_text": "boop", "corrected_text": "book",
         "status": "validated", "error_type": "languagetool"},
        {"para_id": "p2", "original_text": "teh", "corrected_text": "the",
         "status": "query", "force_query": True},          # a query, not an edit
        {"para_id": "p3", "original_text": "x", "corrected_text": "x",
         "status": "validated"},                            # a no-op
        {"para_id": "p4", "original_text": "alot", "corrected_text": "a lot",
         "applied": True},                                  # applied flag only
    ]}), encoding="utf-8")
    edits = verify.applied_edits(tmp_path)
    assert [e["para_id"] for e in edits] == ["p1", "p4"]


def test_sentence_around_isolates_the_finished_context():
    para = "I paid. I ate quest here. She left."
    assert verify._sentence_around(para, "quest") == "I ate quest here."
    # A span the accepted text no longer contains falls back to the paragraph.
    assert verify._sentence_around(para, "nowhere") == para


# --- change verifier ----------------------------------------------------------

def test_verify_changes_reports_only_flagged_edits_and_maps_the_index():
    edits = [
        {"para_id": "p1", "original_text": "queso", "corrected_text": "quest",
         "error_type": "languagetool"},
        {"para_id": "p2", "original_text": "5-6", "corrected_text": "5–6",
         "error_type": "sweep_dash"},
    ]
    accepted = {"p1": "I ate quest here.", "p2": "from 5–6 times"}
    prov = _Provider({"problems": [
        {"index": 1, "verdict": "voice_damage",
         "detail": "queso is a food; quest is a nonsense swap.", "fix": "queso"}]})
    usage = Usage()
    problems = verify.verify_changes(edits, accepted, prov, "m", usage)
    assert len(problems) == 1
    p = problems[0]
    assert (p.para_id, p.verdict, p.fix) == ("p1", "voice_damage", "queso")
    assert p.original_text == "queso"          # index 1 mapped back to edit 0
    # the finished context, not the source, was handed to the model
    assert "quest" in prov.calls[0]["user"]


def test_verify_changes_ignores_an_out_of_range_index():
    edits = [{"para_id": "p1", "original_text": "a", "corrected_text": "b"}]
    accepted = {"p1": "b"}
    prov = _Provider({"problems": [
        {"index": 9, "verdict": "artifact", "detail": "x", "fix": "y"}]})
    assert verify.verify_changes(edits, accepted, prov, "m", Usage()) == []


def test_verify_changes_batches_and_a_truncated_reply_is_a_loss():
    edits = [{"para_id": f"p{i}", "original_text": "a", "corrected_text": "b"}
             for i in range(3)]
    accepted = {f"p{i}": "b" for i in range(3)}
    prov = _Provider({"problems": []})
    # batch_size 2 → two calls; second returns nothing scripted (empty body ok)
    out = verify.verify_changes(edits, accepted, prov, "m", Usage(), batch_size=2)
    assert out == []
    assert len(prov.calls) == 2


# --- finished-text walk -------------------------------------------------------

def test_walk_finished_text_returns_residuals_and_drops_hallucinated_ids():
    accepted = {"p1": "The the cat sat.", "p2": "All clean here."}
    prov = _Provider({"findings": [
        {"para_id": "p1", "quote": "The the", "problem": "doubled word",
         "suggestion": "The", "severity": "high"},
        {"para_id": "ghost", "quote": "x", "problem": "y", "suggestion": "z",
         "severity": "low"}]})                       # id not in the accepted map
    found = verify.walk_finished_text(accepted, prov, "m", Usage())
    assert len(found) == 1
    assert (found[0].para_id, found[0].severity) == ("p1", "high")


def test_walk_reads_respect_the_char_budget():
    accepted = {f"p{i}": "x" * 100 for i in range(5)}
    reads = verify._walk_reads(accepted, char_budget=250)
    assert [len(r) for r in reads] == [2, 2, 1]        # 2*100 fits, a 3rd overflows


# --- certify hooks ------------------------------------------------------------

def test_certify_change_verify_fails_on_a_recorded_problem(tmp_path):
    (tmp_path / "change_verify.json").write_text(json.dumps({
        "applied_edits": 10,
        "problems": [{"para_id": "p1", "verdict": "voice_damage",
                      "detail": "d", "fix": "f"}]}), encoding="utf-8")
    check = _certify_change_verify(tmp_path)
    assert check.status == "fail" and "voice_damage" in check.detail


def test_certify_change_verify_passes_when_clean_and_skips_when_absent(tmp_path):
    assert _certify_change_verify(tmp_path).status == "skip"
    (tmp_path / "change_verify.json").write_text(
        json.dumps({"applied_edits": 5, "problems": []}), encoding="utf-8")
    assert _certify_change_verify(tmp_path).status == "pass"


def test_certify_finished_walk_fails_on_any_unsettled_residual(tmp_path):
    """Residual settlement (I7): a residual of ANY severity with no settlement
    record blocks delivery; once settlement.json records it, the walk passes.
    (Before settlement existed, low/medium residuals were mere notes.)"""
    from galley.settle import Settlement, SettlementRecord
    from galley.verify import residual_id
    (tmp_path / "finished_walk.json").write_text(json.dumps({
        "residuals": [{"para_id": "p1", "severity": "low", "quote": "q",
                       "problem": "p", "suggestion": "s"}]}), encoding="utf-8")
    check = _certify_finished_walk(tmp_path)
    assert check.status == "fail" and "galley settle" in check.detail
    st = Settlement(run_dir=str(tmp_path), rounds=1)
    st.records.append(SettlementRecord(residual_id("p1", "q"), 1, "query",
                                       None, "", "", "unresolved_after_1",
                                       "deterministic", para_id="p1"))
    st.save(tmp_path)
    assert _certify_finished_walk(tmp_path).status == "pass"


# --- P0 fixes: anchor-located context, deletions, a result that says it ran --

def test_applied_edits_keeps_a_pure_deletion(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [
        {"para_id": "p1", "original_text": " very", "corrected_text": "",
         "status": "validated"},                            # a pure deletion
        {"para_id": "p2", "original_text": "", "corrected_text": ",",
         "status": "validated"},                            # a pure insertion
        {"para_id": "p3", "original_text": "", "corrected_text": "",
         "status": "validated"},                            # nothing on either side
    ]}), encoding="utf-8")
    assert [e["para_id"] for e in verify.applied_edits(tmp_path)] == ["p1", "p2"]


def test_finished_context_locates_the_sentence_by_anchor_not_by_text():
    """A minimal-diff row whose corrected_text is ',' must land on ITS
    sentence — text.find(',') finds the first sentence with a comma."""
    original = {"p1": "First, we sat. Then we ate quest here. Last, we left."}
    accepted = {"p1": "First, we sat. Then, we ate quest here. Last, we left."}
    edit = {"para_id": "p1", "original_text": "", "corrected_text": ",",
            "anchor": {"start": 19, "end": 19, "delete_text": "",
                       "insert_text": ","}}
    before, after = verify._finished_context(edit, 19, original, accepted)
    assert before == "Then we ate quest here."
    assert after == "Then, we ate quest here."


def test_finished_context_shifts_later_edits_by_earlier_ones_in_the_paragraph():
    original = {"p1": "aa bb cc. dd ee ff."}
    accepted = {"p1": "aaaa bb cc. dd ee."}
    edits = [
        {"para_id": "p1", "original_text": "aa", "corrected_text": "aaaa",
         "anchor": {"start": 0, "end": 2, "delete_text": "aa",
                    "insert_text": "aaaa"}},
        {"para_id": "p1", "original_text": "dd ee ff", "corrected_text": "dd ee",
         "anchor": {"start": 15, "end": 18, "delete_text": " ff",
                    "insert_text": ""}},
    ]
    starts = verify._accepted_starts(edits)
    assert starts == [0, 17]
    before, after = verify._finished_context(edits[1], starts[1], original, accepted)
    assert (before, after) == ("dd ee ff.", "dd ee.")


def test_finished_context_hunts_nearby_when_the_paragraph_drifted():
    """An untracked change shifted the paragraph: the arithmetic offset misses,
    the inserted text is found at the nearest occurrence instead."""
    original = {"p1": "One thing. Two thing here. Three."}
    accepted = {"p1": "One BIG thing. Two things here. Three."}
    edit = {"para_id": "p1", "original_text": "thing here",
            "corrected_text": "things here",
            "anchor": {"start": 15, "end": 20, "delete_text": "thing",
                       "insert_text": "things"}}
    before, after = verify._finished_context(edit, 15, original, accepted)
    assert (before, after) == ("Two thing here.", "Two things here.")


def test_finished_context_falls_back_to_text_search_without_an_anchor():
    accepted = {"p1": "I paid. I ate quest here."}
    edit = {"para_id": "p1", "original_text": "queso", "corrected_text": "quest"}
    before, after = verify._finished_context(edit, None, {}, accepted)
    assert (before, after) == ("queso", "I ate quest here.")


def test_finished_context_composes_the_after_sentence_without_an_accepted_view():
    original = {"p1": "One. Two thing here. Three."}
    edit = {"para_id": "p1", "original_text": "thing", "corrected_text": "things",
            "anchor": {"start": 9, "end": 14, "delete_text": "thing",
                       "insert_text": "things"}}
    before, after = verify._finished_context(edit, None, original, {})
    assert (before, after) == ("Two thing here.", "Two things here.")


def test_verify_changes_presents_the_before_after_pair_and_reads_deletions():
    original = {"p1": "She was very, very tired. Then she slept."}
    accepted = {"p1": "She was very tired. Then she slept."}
    edits = [{"para_id": "p1", "original_text": ", very", "corrected_text": "",
              "error_type": "repetition",
              "anchor": {"start": 12, "end": 18, "delete_text": ", very",
                         "insert_text": ""}}]
    prov = _Provider({"problems": []})
    verify.verify_changes(edits, accepted, prov, "m", Usage(), original=original)
    user = prov.calls[0]["user"]
    assert "before: She was very, very tired." in user
    assert "now reads: She was very tired." in user
    assert "edit: ', very' -> ''" in user


def test_verify_run_with_no_deliverable_reports_it_did_not_run(tmp_path):
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [
        {"para_id": "p1", "original_text": "a", "corrected_text": "b",
         "status": "validated"}]}), encoding="utf-8")
    prov = _Provider({"problems": []}, {"findings": []})
    result = verify.verify_run(tmp_path, prov, "m", Usage())
    assert result.ran_changes is False and result.ran_walk is False
    assert "no accepted text" in result.reason
    assert prov.calls == []                     # nothing was read, nothing spent
    problems, residuals = result                # the old tuple shape still works
    assert (problems, residuals) == ([], [])


def test_verify_run_on_a_real_deliverable_reports_both_gates_ran(tmp_path):
    import shutil
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "tiny_novel.docx"
    shutil.copy(fixture, tmp_path / "book.docx")
    (tmp_path / "findings.json").write_text(json.dumps({"findings": []}),
                                            encoding="utf-8")
    prov = _Provider(*([{"findings": []}] * 50))
    result = verify.verify_run(tmp_path, prov, "m", Usage(), run_changes=False)
    assert result.ran_changes is False and result.ran_walk is True
    assert result.reason == ""
    assert prov.calls                           # the walk actually read the text
