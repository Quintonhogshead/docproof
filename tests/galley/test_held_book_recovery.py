"""Regression coverage for the live Aragon/Bradshaw engine holds."""
import hashlib
import json

import pytest

from docproof.__main__ import main
from docproof.models import Usage
from galley.comment_reconcile import actual_comments
from galley.settle import open_items, unsettled
from galley.verify import (ResidualFinding, VerifyRunResult, deliverable_docx,
                           write_artifacts, residual_id)
from tests.galley.test_settle import (_accepted, _build, _manuscript, _para_ids,
                                     _records, _replay_config, _settle, _walk, _Provider)


def test_residual_restoring_source_withdraws_owner_without_losing_other_edits(tmp_path):
    src = _manuscript(tmp_path, ["The book 1 reference is in teh first sentence."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [
        {"para_id": pid, "original_text": "book 1", "corrected_text": "book one"},
        {"para_id": pid, "original_text": "teh", "corrected_text": "the"}])
    _walk(run, [{"para_id": pid, "quote": "book one reference",
                 "suggestion": "book 1 reference", "problem": "Wrong numeral edit"}])
    assert _settle(tmp_path, run, src, "--mechanical-only") == 0
    assert _accepted(run)[pid] == "The book 1 reference is in the first sentence."
    recs, st = _records(run)
    assert recs[residual_id(pid, "book one reference")].reason == "restores_source"
    assert not st.open
    assert all(r.get("status") != "rejected_noop" for r in
               json.loads((run / "findings.json").read_text())["findings"])


def test_locked_instructions_duplicates_and_regenerated_sweep_queries_are_dropped(tmp_path):
    para = "Lorem dolor.Excepteur that that dolor. Neque porro. Neque porro."
    src = _manuscript(tmp_path, [para])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    zones = tmp_path / "zones.json"
    zones.write_text(json.dumps({"zones": [{"para_ids": [pid], "permission": "locked"}]}))
    _walk(run, [
        {"para_id": pid, "quote": "dolor.Excepteur", "problem": "Missing space",
         "suggestion": "Restore the paragraph before the appended block"},
        {"para_id": pid, "quote": "Neque porro.", "problem": "Duplicate sentence",
         "suggestion": "Delete one copy"},
        {"para_id": pid, "quote": "that that", "problem": "Repeated word", "suggestion": ""}])
    cfg = _replay_config(tmp_path, intent_zones_file=str(zones))
    assert main(["galley", "settle", str(run), "--source", str(src),
                 "--config", cfg, "--engine", "none", "--no-verify"]) == 0
    assert _accepted(run)[pid] == para
    assert actual_comments(deliverable_docx(run)) == []
    assert all(r.action == "drop" and r.reason.startswith("intent_zone")
               for r in _records(run)[0].values())


def test_locked_term_does_not_hide_an_error_elsewhere_in_a_quoted_sentence(tmp_path):
    from docproof.intent_zones import IntentZone, ResolvedZones
    zone = IntentZone(terms=["Mom"], permission="locked")
    zones = ResolvedZones({"p": [(0, 3, zone)]})
    assert zones.locked_cover("p", 0, 3)
    assert not zones.locked_cover("p", 0, 30)
    assert not zones.locked_cover("p", 4, 7)


def test_separate_verification_pass_enters_settlement_and_keeps_its_binding(tmp_path):
    src = _manuscript(tmp_path, ["The letter was hard to recieve."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    _walk(run, [])
    second = tmp_path / "verify" / "type-compare"
    finding = ResidualFinding(pid, "recieve", "Misspelling", "receive", "high")
    write_artifacts(second, VerifyRunResult([], [], False, False),
                    VerifyRunResult([], [finding], False, True),
                    model="test", engine="none", usage_changes=Usage(),
                    usage_walk=Usage(), applied=0, paragraphs=1, source_run_dir=run)
    before = (second / "finished_walk.json").read_bytes()
    assert [r.id for r in open_items(run)] == [finding.id]
    assert unsettled(run) == ([finding.id], [])
    assert _settle(tmp_path, run, src) == 0
    assert "receive" in _accepted(run)[pid]
    assert not open_items(run)
    assert (second / "finished_walk.json").read_bytes() == before
    assert json.loads((run / "finished_walk.json").read_text())["unverified_paragraphs"] == [pid]


@pytest.mark.parametrize("invalid", [None, "build", "quote", "basis", "unknown"])
def test_practitioner_query_requires_current_exact_evidence(tmp_path, invalid):
    src = _manuscript(tmp_path, ["She took it without meaning like one."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    quote = "without meaning like one"
    _walk(run, [{"para_id": pid, "quote": quote, "problem": "Missing intended wording",
                 "suggestion": ""}])
    data = {"build_sha256": hashlib.sha256(deliverable_docx(run).read_bytes()).hexdigest(),
            "queries": [{"residual_id": residual_id(pid, quote), "para_id": pid,
                         "quote": quote, "question": "Is a word missing after ‘meaning’ here?",
                         "missing_knowledge": "The author's intended wording"}]}
    if invalid == "build":
        data["build_sha256"] = "old"
    if invalid == "quote":
        data["queries"][0]["quote"] = "different quote"
    if invalid == "basis":
        data["queries"][0]["missing_knowledge"] = ""
    if invalid == "unknown":
        data["queries"][0]["residual_id"] = "r-unknown"
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(data))
    original = deliverable_docx(run).read_bytes()
    rc = _settle(tmp_path, run, src, "--queries", str(path))
    if invalid:
        assert rc == 2
        assert deliverable_docx(run).read_bytes() == original
        assert not (run / "settlement.json").exists()
    else:
        assert rc == 0
        assert _accepted(run)[pid] == "She took it without meaning like one."
        assert len(actual_comments(deliverable_docx(run))) == 1
        rec = _records(run)[0][residual_id(pid, quote)]
        assert rec.action == "query"
        assert rec.reason.startswith("author_knowledge:")
        from galley.manifest import _certify_settlement
        from docproof.utils.xml_helpers import DocxPackage
        assert _certify_settlement(run).status == "pass"
        package = DocxPackage(deliverable_docx(run))
        package.tree("word/comments.xml").clear()
        package.mark_modified("word/comments.xml")
        package.save(deliverable_docx(run))
        assert _certify_settlement(run).status == "fail"


def test_missing_registered_pass_stays_a_blocker(tmp_path):
    from galley.settlement_inputs import register_verification_source, verification_dirs
    src = _manuscript(tmp_path)
    run = _build(tmp_path, src, [])
    second = tmp_path / "second"
    second.mkdir()
    _walk(second, [])
    register_verification_source(run, second)
    # The durable snapshot survives deletion or reuse of the convenience output.
    (second / "finished_walk.json").unlink()
    assert open_items(run) == []
    (verification_dirs(run)[1] / "finished_walk.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        open_items(run)


def test_source_restoration_retries_another_residual_once_its_quote_returns(tmp_path):
    from docproof.config import load_config
    from galley.settle import Settler, SettleOptions
    src = _manuscript(tmp_path, ["The book 1 reference is correct."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [
        {"para_id": pid, "original_text": "book 1", "corrected_text": "book one"}])
    _walk(run, [
        {"para_id": pid, "quote": "book one reference", "suggestion": "book 1 reference",
         "problem": "The locator keeps its numeral"},
        {"para_id": pid, "quote": "book 1 reference", "suggestion": "",
         "problem": "Should this digit be spelled out?"}])
    judge = _Provider({"action": "drop", "reason": "The part-of-book locator is correct"})
    from pathlib import Path
    result = Settler(run, cfg=load_config(_replay_config(tmp_path)), manuscript=src,
                     error_dir=Path("config/error_types"), provider=judge,
                     options=SettleOptions(verify_delta=False, mechanical_only=True)).run()
    assert not result.open
    assert len(judge.calls) == 1
    assert _accepted(run)[pid] == "The book 1 reference is correct."
