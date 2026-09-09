"""Exact metadata repairs must resume without another editorial generation."""
import copy
import json

import pytest

from galley import astra_review as ar, astra_reconcile as rec
from .test_astra_reconcile import run, _freeze, _action, _parts


OLD = 'Subject/verb agreement reads as an error against the plural complement “molds and slimes.”'
NEW = '“Spores” names reproductive particles; “spoors” denotes tracks or scent trails.'


def prepare(run, *, comment=True):
    payload = json.loads((run / "findings.json").read_text())
    payload["findings"][0].update(para_id="body-0002", explanation=OLD,
        corrected_text="Historical text that must never overwrite the manuscript.",
        anchor={"delete_text": "or", "insert_text": "re"})
    ar._atomic(run / "findings.json", payload)

    def configure(review, packet):
        repair = _action("internal_repair", "body-0002", quote=OLD, replacement=NEW)
        repair["finding_ids"] = ["finding-000001"]
        review["actions"] = [repair]
        review["finding_review"]["exceptions"] = [{"finding_id": "finding-000001",
            "action": "internal_repair", "reason": "The explanation describes a different correction."}]
        if comment:
            action = _action("replace_comment", "body-0001", comment_ids=["8"],
                             replacement="Which return is intended?")
            action["id"] = "replace-question"
            review["actions"].append(action)
            review["comment_decisions"][1]["action"] = "replace_question"
    return _freeze(run, configure)


@pytest.mark.parametrize("comment", [False, True])
def test_exact_metadata_repair_preserves_manuscript_and_frozen_evidence(run, comment):
    frozen, receipt = prepare(run, comment=comment)
    original_packet = (run / ar.PACKET_FILE).read_bytes()
    original_review = (run / ar.RECEIPT_FILE).read_bytes()
    original_parts = _parts(run / "book.docx")
    result = rec.reconcile_run(run)
    assert result["delivery_ready"] and not result["repair_required"]
    current = ar.build_packet(run)
    expected = copy.deepcopy(frozen["artifacts"])
    expected["findings.json"]["findings"][0]["explanation"] = NEW
    assert current["artifacts"] == expected
    assert current["accepted_paragraphs"] == frozen["accepted_paragraphs"]
    assert current["revisions"] == frozen["revisions"]
    assert _parts(run / "book.docx")["word/document.xml"] == original_parts["word/document.xml"]
    assert (run / ar.PACKET_FILE).read_bytes() == original_packet
    assert (run / ar.RECEIPT_FILE).read_bytes() == original_review
    assert len(result["reconciliation"]["metadata_changes"]) == 1
    assert rec.reconcile_run(run) == result
    assert ar.validate_receipt(run) == result


@pytest.mark.parametrize("change,match", [
    ({"quote": OLD[:-1]}, "exact finding explanation"),
    ({"quote": ""}, "exact finding explanation"),
    ({"quote": OLD.replace("“", '"')}, "exact finding explanation"),
    ({"para_id": "body-0003"}, "different paragraph"),
    ({"finding_ids": []}, "one exact finding"),
    ({"finding_ids": ["finding-000001", "finding-000001"]}, "one exact finding"),
    ({"finding_ids": ["unknown"]}, "unknown evidence IDs"),
    ({"replacement": " "}, "nonempty changed explanation"),
    ({"replacement": OLD}, "nonempty changed explanation"),
    ({"kind": "edit_text"}, "quote is absent from accepted paragraph"),
])
def test_metadata_authorization_rejects_ambiguous_wrong_or_noop_targets(run, change, match):
    packet, receipt = prepare(run)
    review = copy.deepcopy(receipt["review"])
    review["actions"][0].update(change)
    with pytest.raises(ar.AstraReviewError, match=match):
        ar.validate_review(review, packet)


def test_duplicate_metadata_writes_are_rejected(run):
    packet, receipt = prepare(run)
    review = copy.deepcopy(receipt["review"])
    duplicate = dict(review["actions"][0], id="second-repair")
    review["actions"].append(duplicate)
    with pytest.raises(ar.AstraReviewError, match="more than once"):
        ar.validate_review(review, packet)


@pytest.mark.parametrize("stage", ["before_document", "before_metadata", "after_metadata"])
def test_crashes_resume_through_receipt_validation_without_rewriting_review(run, monkeypatch, stage):
    prepare(run)
    original_review = (run / ar.RECEIPT_FILE).read_bytes()
    original_atomic, original_replace = rec._atomic, rec.os.replace

    def atomic(path, data):
        if ((stage == "before_metadata" and path.name == "findings.json") or
                (stage == "after_metadata" and path.name == rec.RECONCILIATION_FILE
                 and data.get("status") == "completed")):
            raise RuntimeError("simulated crash")
        return original_atomic(path, data)

    def replace(source, target):
        if stage == "before_document" and str(target).endswith("book.docx"):
            raise RuntimeError("simulated crash")
        return original_replace(source, target)

    monkeypatch.setattr(rec, "_atomic", atomic)
    monkeypatch.setattr(rec.os, "replace", replace)
    with pytest.raises(RuntimeError, match="simulated crash"):
        rec.reconcile_run(run)
    assert ar._load(run / rec.RECONCILIATION_FILE)["status"] == "prepared"
    monkeypatch.setattr(rec, "_atomic", original_atomic)
    monkeypatch.setattr(rec.os, "replace", original_replace)
    result = ar.validate_receipt(run)
    assert result["delivery_ready"] and result["reconciliation"]["status"] == "completed"
    assert ar._load(run / "findings.json")["findings"][0]["explanation"] == NEW
    assert (run / ar.RECEIPT_FILE).read_bytes() == original_review


@pytest.mark.parametrize("field,value", [("explanation", "Unapproved explanation"),
    ("corrected_text", "Unapproved manuscript"), ("status", "dropped")])
def test_post_repair_evidence_tampering_blocks_delivery(run, field, value):
    prepare(run)
    rec.reconcile_run(run)
    findings = ar._load(run / "findings.json")
    findings["findings"][0][field] = value
    ar._atomic(run / "findings.json", findings)
    with pytest.raises(ar.AstraReviewError, match="outside the approved metadata plan"):
        ar.validate_receipt(run)


def test_chunk_finding_ids_remain_paired_when_ownership_order_differs(run):
    from galley import astra_subscription as sub
    packet, _ = prepare(run)
    packet["finding_ids"].append("finding-000002")
    packet["artifacts"]["findings.json"]["findings"].append({
        "para_id": "body-0001", "explanation": "A different finding."})
    ids = {"paragraphs": [p["id"] for p in packet["accepted_paragraphs"]],
           "findings": ["finding-000002", "finding-000001"],
           "comments": [], "revisions": [], "issues": []}
    local = sub._local_packet({"owned_ids": ids, "supporting_paragraphs": []}, packet)
    rows = dict(zip(local["finding_ids"], local["artifacts"]["findings.json"]["findings"]))
    assert rows["finding-000001"]["para_id"] == "body-0002"
    assert rows["finding-000001"]["explanation"] == OLD
    assert rows["finding-000002"]["para_id"] == "body-0001"


def test_cached_chunk_recovers_then_final_review_and_metadata_repair_resume(run, monkeypatch):
    from galley import astra_subscription as sub
    from docproof.utils.xml_helpers import DocxPackage
    from .test_astra_subscription import Runner

    package = DocxPackage(run / "book.docx")
    styles = package.tree("word/styles.xml")
    for child in list(styles):
        styles.remove(child)
    package.mark_modified("word/styles.xml")
    package.save(run / "book.docx")
    packet, receipt = prepare(run, comment=False)
    (run / ar.RECEIPT_FILE).unlink()
    (run / ar.PACKET_FILE).unlink()
    final_review = copy.deepcopy(receipt["review"])

    def mutate(result):
        if "review" in result:
            result["review"] = copy.deepcopy(final_review)
        elif "finding-000001" in result["reviewed_ids"]["findings"]:
            action = copy.deepcopy(final_review["actions"][0])
            action["id"] = result["chunk_id"] + "-repair-explanation"
            final_review["actions"][0]["id"] = action["id"]
            result["actions"] = [action]
            result["finding_review"] = copy.deepcopy(final_review["finding_review"])

    generator = Runner(packet, mutate=mutate)
    cache = {}

    def cached_runner(prompt, schema, **kwargs):
        request_id = kwargs["request_id"]
        if request_id not in cache:
            cache[request_id] = generator(prompt, schema, **kwargs)
        return copy.deepcopy(cache[request_id])

    validate = sub._validate_chunk

    def old_validator(review, *args, **kwargs):
        if any(a["kind"] == "internal_repair" for a in review["actions"]):
            raise ar.AstraReviewError("old manuscript-only quote check")
        return validate(review, *args, **kwargs)

    monkeypatch.setattr(sub, "_validate_chunk", old_validator)
    with pytest.raises(ar.AstraReviewError, match="old manuscript-only"):
        sub.review_run(run, runner=cached_runner)
    generated_before = len(generator.calls)
    monkeypatch.setattr(sub, "_validate_chunk", validate)
    review = sub.review_run(run, runner=cached_runner)
    assert len(generator.calls) == generated_before + 1  # Only final adjudication is new.
    assert review["repair_required"]
    result = rec.reconcile_run(run)
    assert result["delivery_ready"]
    assert sub.review_run(run, runner=lambda *a, **kw: pytest.fail("regenerated a cached review")) == result


def test_concurrent_reconciliation_is_rejected(run):
    import fcntl
    prepare(run)
    with (run / ".astra-reconcile.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ar.AstraReviewError, match="Another Astra reconciliation"):
            rec.reconcile_run(run)
