"""Verification retries must preserve accurate coverage of the current book."""
import json

import docx
import pytest

import docproof.__main__ as cli
from docproof.providers.base import ProviderResult
from docproof.models import Usage
from galley import verify
from galley.manifest import _certify_change_verify, _certify_finished_walk


class Provider:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        body = self.bodies.pop(0) if self.bodies else {kwargs["schema_name"]: []}
        return body if isinstance(body, ProviderResult) else ProviderResult(
            parsed=body, stop_reason="ok")


@pytest.fixture(autouse=True)
def isolated_coverage(monkeypatch):
    monkeypatch.setattr(verify, "UNREAD", [])
    monkeypatch.setattr(verify, "UNREAD_BATCHES", [])
    monkeypatch.setattr(verify, "_LOSSES", [])


def build(run, paragraphs):
    run.mkdir(exist_ok=True)
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(run / "book.docx")
    (run / "findings.json").write_text(json.dumps({
        "findings": [], "generated_at": "2000-01-01T00:00:00+00:00",
        "cost": {"total_usd": 0},
    }))
    return list(verify.accepted_text(run))


def read(run, monkeypatch, provider, *extra):
    monkeypatch.setattr(cli, "build_provider", lambda *a, **kw: provider)
    return cli.main(["galley", "verify", str(run), "--config",
                     "config/default.yaml", "--model", "gpt-5.6-luna", *extra])


def artifact(run, name="finished_walk.json"):
    return json.loads((run / name).read_text())


def test_initial_partial_read_covers_only_selected_paragraphs(tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence.", "The the teh bad sentence."])
    provider = Provider()
    assert read(run, monkeypatch, provider, "--paragraphs", ids[0]) == 0
    assert len(provider.calls) == 1
    assert ids[1] not in provider.calls[0]["user"]
    assert artifact(run)["unverified_paragraphs"] == [ids[1]]
    assert _certify_finished_walk(run).status == "fail"
    assert _certify_change_verify(run).status == "fail"
    # Completing coverage through another delta is sufficient.
    assert read(run, monkeypatch, provider, "--paragraphs", ids[1]) == 0
    assert _certify_finished_walk(run).status == "pass"
    assert _certify_change_verify(run).status == "pass"


def test_full_reread_replaces_old_findings_and_build_binding(tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["The teh dog.", "The cat."])
    provider = Provider({"findings": [{
        "para_id": ids[0], "quote": "teh", "problem": "typo",
        "suggestion": "the", "severity": "high",
    }]})
    assert read(run, monkeypatch, provider) == 1
    old_hash = artifact(run)["accepted_sha256"]
    build(run, ["The dog.", "The cat."])
    assert read(run, monkeypatch, provider) == 0
    result = artifact(run)
    assert result["residuals"] == []
    assert result["accepted_sha256"] != old_hash
    assert result["accepted_sha256"] == verify.build_fingerprints(run)["accepted_sha256"]
    assert _certify_finished_walk(run).status == "pass"


def test_full_reread_recovers_failed_coverage(tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence.", "Another sentence."])
    failed = ProviderResult(parsed=None, stop_reason="max_tokens")
    provider = Provider(failed, failed)
    read(run, monkeypatch, provider)
    assert artifact(run)["unread_paragraphs"] == ids
    assert _certify_finished_walk(run).status == "fail"
    assert read(run, monkeypatch, provider) == 0
    assert artifact(run)["unread_paragraphs"] == []
    assert artifact(run)["unverified_paragraphs"] == []
    assert _certify_finished_walk(run).status == "pass"


def test_partial_reread_does_not_cover_changes_elsewhere(tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence.", "Another sentence."])
    provider = Provider()
    assert read(run, monkeypatch, provider) == 0
    build(run, ["Changed first sentence.", "Changed second sentence."])
    assert read(run, monkeypatch, provider, "--paragraphs", ids[0]) == 0
    assert artifact(run)["unverified_paragraphs"] == [ids[1]]
    assert _certify_finished_walk(run).status == "fail"


def test_certification_checks_paragraph_coverage_even_with_matching_book_hash(
        tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence.", "Another sentence."])
    assert read(run, monkeypatch, Provider()) == 0
    result = artifact(run)
    del result["paragraph_sha256"][ids[1]]
    (run / "finished_walk.json").write_text(json.dumps(result))
    assert _certify_finished_walk(run).status == "fail"
    assert "lack verification" in _certify_finished_walk(run).detail


def test_full_changes_only_reread_preserves_walk_evidence(tmp_path, monkeypatch):
    run = tmp_path / "run"
    ids = build(run, ["The teh dog."])
    provider = Provider({"findings": [{
        "para_id": ids[0], "quote": "teh", "problem": "typo",
        "suggestion": "the", "severity": "high",
    }]})
    assert read(run, monkeypatch, provider) == 1
    old = artifact(run)
    assert read(run, monkeypatch, provider, "--changes-only") == 0
    new = artifact(run)
    assert new["residuals"] == old["residuals"]
    assert new["accepted_sha256"] == old["accepted_sha256"]


def test_walk_coverage_is_independent_of_a_failed_change_batch(tmp_path):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence."])
    verify.UNREAD_BATCHES[:] = [{"index": 1, "edits": 1, "para_ids": ids}]
    result = verify.VerifyRunResult([], [], ran_changes=True, ran_walk=True)
    verify.write_artifacts(
        run, result, result, model="gpt-5.6-luna", engine="provider",
        usage_changes=Usage(), usage_walk=Usage(), applied=1, paragraphs=1,
        para_ids=ids, merge=True)
    assert _certify_change_verify(run).status == "fail"
    assert _certify_finished_walk(run).status == "pass"


def test_partial_changes_only_keeps_skipped_walk_unread_coverage(tmp_path):
    run = tmp_path / "run"
    ids = build(run, ["Good sentence."])
    verify.UNREAD[:] = ids
    result = verify.VerifyRunResult([], [], ran_changes=True, ran_walk=True)
    verify.write_artifacts(
        run, result, result, model="gpt-5.6-luna", engine="provider",
        usage_changes=Usage(), usage_walk=Usage(), applied=0, paragraphs=1)
    verify.UNREAD.clear()
    skipped = verify.VerifyRunResult([], [], ran_changes=True, ran_walk=False)
    verify.write_artifacts(
        run, skipped, skipped, model="gpt-5.6-luna", engine="provider",
        usage_changes=Usage(), usage_walk=Usage(), applied=0, paragraphs=1,
        para_ids=ids, merge=True)
    assert artifact(run)["unread_paragraphs"] == ids
    assert _certify_finished_walk(run).status == "fail"


def test_change_batch_can_be_recovered_in_separate_paragraph_reads(tmp_path):
    run = tmp_path / "run"
    ids = build(run, ["First sentence.", "Second sentence."])
    verify.UNREAD_BATCHES[:] = [{"index": 1, "edits": 2, "para_ids": ids}]
    result = verify.VerifyRunResult([], [], ran_changes=True, ran_walk=True)
    verify.write_artifacts(
        run, result, result, model="gpt-5.6-luna", engine="provider",
        usage_changes=Usage(), usage_walk=Usage(), applied=2, paragraphs=2)
    verify.UNREAD_BATCHES.clear()
    for pid in ids:
        verify.write_artifacts(
            run, result, result, model="gpt-5.6-luna", engine="provider",
            usage_changes=Usage(), usage_walk=Usage(), applied=2, paragraphs=2,
            para_ids=[pid], merge=True)
    assert artifact(run, "change_verify.json")["unread_batches"] == []
    assert _certify_change_verify(run).status == "pass"
