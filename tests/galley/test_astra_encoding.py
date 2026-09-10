"""Exact evidence preservation, bounded delivery and saved-contract compatibility."""
import copy
import json

import pytest

from galley import astra_encoding as enc
from galley import astra_review as ar
from galley import astra_subscription as sub
from .test_astra_subscription import run, cross_book_run, Runner


def test_encoding_roundtrips_xml_optional_fields_and_literal_marker_collisions():
    node = {"tag": "{urn:custom}node", "attributes": {"xml:space": "preserve", "{urn:other}x": "<&\""},
            "text": "", "tail": " 🦋\n ", "children": [
                {"tag": "w:empty"}, {"tag": "w:empty", "attributes": {}, "text": "", "tail": "", "children": []},
                {"tag": "w:t", "text": "author evidence " * 50}]}
    source = {"styles": node, "property_definitions": {"p1": node},
              "literal": [{"$ref": "d0"}, {"$table": {"columns": ["bad"], "rows": [[1]]}},
                          {"$xml": ["not markup"]}, {"$object": [["k", 1]]}, {"$runs": [[0, 1, "x"]]}],
              "optional_rows": [{"id": "a", "value": None}, {"id": "b"}, {"id": "c", "value": []}],
              "runs": [{"start": 0, "end": 10, "properties": "p1"}],
              "other_nodes": [{"tag": "not canonical XML", "text": None}, {"tag": "x", "extra": True}],
              "empty": [None, [], {}, "", 0, False]}
    original = copy.deepcopy(source)
    packed = enc.pack_evidence(source)
    assert enc.unpack_evidence(json.loads(ar._json(packed))) == original
    assert source == original
    assert packed == enc.pack_evidence(source)
    assert "$xml" in ar._json(packed) and "$ref" in ar._json(packed)
    assert "urn:custom" in ar._json(packed)


def test_all_records_reconstruct_with_original_hashes_and_bound(cross_book_run):
    packet = ar.build_packet(cross_book_run)
    original = copy.deepcopy(packet)
    plan = sub.plan_review(packet, max_chunk_bytes=40_000)
    kinds = set()
    for chunk in plan["chunks"]:
        encoded = sub.chunk_for_model(chunk)
        expanded = sub.chunk_from_model(json.loads(ar._json(encoded)))
        assert expanded == chunk
        assert sub._bytes(encoded) <= sub._bytes(chunk) <= 24_000
        assert expanded["chunk_sha256"] == ar._hash({k: v for k, v in expanded.items() if k != "chunk_sha256"})
        kinds.update(record["kind"] for record in expanded["records"])
    assert {"paragraphs", "revisions", "comments", "findings", "supplemental"} <= kinds
    assert packet == original


def test_full_styles_supplemental_and_repeated_formatting_remain_exact(run):
    packet = ar.build_packet(run)
    packet["artifacts"]["finished_walk.json"]["residuals"] = [{"para_id": "body-0000", "problem": "example"}]
    packet["issue_index"] = [{"id": "issue-000001", "recorded_id": None,
                              "sources": [{"artifact": "finished_walk.json", "collection": "residuals", "index": 0}]}]
    packet["counts"]["issues"] = 1
    packet["issue_sha256"] = ar._hash(packet["issue_index"])
    packet["packet_sha256"] = ar._hash({k: v for k, v in packet.items() if k != "packet_sha256"})
    plan = sub.plan_review(packet)
    raw, compressed = 0, 0
    decoded_records = []
    for chunk in plan["chunks"]:
        encoded = sub.chunk_for_model(chunk)
        decoded = sub.chunk_from_model(encoded)
        assert decoded == chunk
        decoded_records.extend(decoded["records"])
        raw += sub._bytes(chunk)
        compressed += sub._bytes(encoded)
    assert compressed < raw * .9
    assert any(record["kind"] == "issues" for record in decoded_records)
    supplemental = [r["value"] for r in decoded_records if r["kind"] == "supplemental"]
    assert any("styles" in part["path"] or "styles" in part.get("value", {}) for part in supplemental)


@pytest.mark.parametrize("bad", [
    {"definitions": {"d0": {"$ref": "d0"}}, "data": {"$ref": "d0"}},
    {"definitions": {}, "data": {"$ref": "missing"}},
    {"definitions": {}, "data": {"$table": {"columns": ["x", "x"], "rows": [[1, 2]]}}},
    {"definitions": {}, "data": {"$table": {"columns": ["x"], "rows": [[]]}}},
])
def test_malformed_reference_or_table_fails_operationally(bad):
    with pytest.raises(ar.AstraReviewError, match="Invalid subscription"):
        enc.unpack_evidence({"encoding_version": 1, **bad})


def test_legacy_contract_hash_is_exact_audit_baseline(run):
    plan = sub.plan_review(ar.build_packet(run), subscription_version=1)
    assert "subscription_version" not in plan
    assert plan["subscription_contract_sha256"] == "c79f0a8229d1cc1b9d3d58a0da540f59620e0fe53f2a259aefb269864ecc7c8e"
    assert ar._hash(sub.LEGACY_CHUNK_PROMPT) == "0642d4b7096948ddd6e273b90cf7438f5c47ed9ad5c9e31428b82621a735458d"
    assert ar._hash(sub.LEGACY_FINAL_PROMPT) == "931aa5871b186019fd1a19060c75925410f0fc9aebd8061f69a8be069d4eeb78"


@pytest.mark.parametrize("failed_at", [1, 2, "final"])
def test_legacy_pending_and_completed_requests_resume_exactly(run, failed_at):
    packet = ar.build_packet(run)
    plan = sub.plan_review(packet, subscription_version=1)
    directory = run / sub.DIRECTORY
    directory.mkdir()
    ar._atomic(directory / "plan.json", plan)
    ar._atomic(run / ar.RECEIPT_FILE, {"status": "pending", "transport": "codex",
        "packet_sha256": packet["packet_sha256"], "max_chunk_bytes": sub.DEFAULT_MAX_CHUNK_BYTES,
        "inputs": {"docx_path": None, "context_paths": []}})
    target = len(plan["chunks"]) + 1 if failed_at == "final" else failed_at
    generated = Runner(packet)
    cache, observed = {}, []

    def runner(prompt, schema, **kwargs):
        key = kwargs["request_id"]
        observed.append(key)
        if key not in cache:
            result = generated(prompt, schema, **kwargs)
            cache[key] = (prompt, copy.deepcopy(schema), result)
            # Simulate completion saved in Codex, followed by interruption before
            # Galley's validated chunk/adjudication receipt is written.
            if len(cache) == target:
                raise RuntimeError("interrupted after saved completed model result")
        old_prompt, old_schema, result = cache[key]
        assert prompt == old_prompt and schema == old_schema
        return copy.deepcopy(result)

    with pytest.raises(ar.AstraReviewError, match="operationally"):
        sub.review_run(run, runner=runner)
    assert "subscription_version" not in ar._load(run / ar.RECEIPT_FILE)
    result = sub.review_run(run, runner=runner)
    assert result["delivery_ready"] and "subscription_version" not in result
    assert sub.validate_coverage_receipt(run, result, packet) == result
    assert sub.review_run(run, runner=runner) == result
    assert len(generated.calls) == len(plan["chunks"]) + 1
    for prompt, _, _ in generated.calls[:-1]:
        assert prompt.startswith(sub.LEGACY_CHUNK_PROMPT + "\nEVIDENCE\n")
        assert "evidence_encoding" not in json.loads(prompt.split("\nEVIDENCE\n", 1)[1])["chunk"]
    assert generated.calls[-1][0].startswith(sub.LEGACY_FINAL_PROMPT + "\nEVIDENCE\n")


def test_new_contract_persists_and_final_inline_reviews_match_receipt_hashes(run):
    packet = ar.build_packet(run)
    runner = Runner(packet)
    result = sub.review_run(run, runner=runner)
    plan = ar._load(run / sub.DIRECTORY / "plan.json")
    assert result["subscription_version"] == plan["subscription_version"] == 2
    evidence = json.loads(runner.calls[-1][0].split("\nEVIDENCE\n", 1)[1])
    assert "complete_final_input_path" not in evidence and "chunk_review_files" not in evidence
    assert evidence == ar._load(run / sub.DIRECTORY / "final-input.json")
    for review, canonical, bound in zip(evidence["chunk_reviews"], plan["chunks"], evidence["coverage_manifest"]["chunks"]):
        assert review["reviewed_ids"] == canonical["owned_ids"]
        assert review["all_evidence_reviewed"] is True
        assert ar._hash(review) == bound["review_sha256"]
    assert "read EVERY chunk review\nfile" not in runner.calls[-1][0]
    assert "search/read exact source passages" in runner.calls[-1][0]


def test_tampered_version_is_rejected_before_any_new_request(run):
    packet = ar.build_packet(run)
    with pytest.raises(ar.AstraReviewError):
        sub.review_run(run, runner=Runner(packet, fail_at=1))
    path = run / sub.DIRECTORY / "plan.json"
    plan = ar._load(path)
    plan.pop("subscription_version")
    ar._atomic(path, plan)
    runner = Runner(packet)
    with pytest.raises(ar.AstraReviewError, match="versions differ"):
        sub.review_run(run, runner=runner)
    assert not runner.calls


@pytest.mark.parametrize("version", [0, 3, True, "2"])
def test_unknown_contract_versions_fail_closed(run, version):
    with pytest.raises(ar.AstraReviewError, match="Unsupported subscription review version"):
        sub.plan_review(ar.build_packet(run), subscription_version=version)
