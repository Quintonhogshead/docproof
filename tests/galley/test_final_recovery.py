"""Only complete readers and provably unsubmitted API calls may resume."""
import errno
import json

import pytest

from galley import astra_review as ar
from .test_astra_review import Fake, run
from .test_astra_workflow import snapshot, _driver


@pytest.mark.parametrize("mutation", [
    {}, {"ran": False}, {"paragraphs": 0}, {"reason": "incomplete"},
    {"unread_paragraphs": ["body-1"]}, {"unverified_paragraphs": ["body-1"]},
    {"paragraphs_verified": ["body-1"]}, {"accepted_sha256": "stale"},
    {"build_sha256": "stale"}, {"verification_pair_id": "another-read"},
    {"verification_provenance": {"complete": False}},
])
def test_failed_coordinator_cannot_adopt_partial_or_stale_read(snapshot, tmp_path, mutation):
    book, _ws, target = snapshot
    driver = _driver(book, tmp_path, astra_review=True)
    assert driver._review_snapshot_available()
    artifact = target / "finished_walk.json"
    value = json.loads(artifact.read_text())
    value.update(mutation)
    artifact.write_text(json.dumps(value if mutation else {}))
    assert not driver._review_snapshot_available()


def test_completed_noisy_read_is_available_for_final_adjudication(snapshot, tmp_path):
    book, _ws, target = snapshot
    artifact = target / "finished_walk.json"
    value = json.loads(artifact.read_text())
    value["residuals"] = [{"para_id": "body-1", "problem": "Needs adjudication"}]
    artifact.write_text(json.dumps(value))
    assert _driver(book, tmp_path, astra_review=True)._review_snapshot_available()


def fail_copy_once(monkeypatch, error=None):
    original = ar.shutil.copyfile
    calls = []
    def copy(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise error or OSError(errno.EAGAIN, "temporary file unavailable")
        return original(*args, **kwargs)
    monkeypatch.setattr(ar.shutil, "copyfile", copy)


def test_unsubmitted_preflight_resumes_same_identity_and_original_output_cap(run, monkeypatch):
    fail_copy_once(monkeypatch)
    client = Fake()
    with pytest.raises(ar.AstraRetryableError):
        ar.review_run(run, budget_usd=100, max_output_tokens=300, client=client)
    before = json.loads((run / ar.RECEIPT_FILE).read_text())
    assert before["submitted"] is False and not client.calls
    result = ar.review_run(run, budget_usd=1000, max_output_tokens=3000, client=client)
    assert result["delivery_ready"] and len(client.calls) == 1
    assert client.calls[0]["max_output_tokens"] == 300
    assert result["resource_operation_id"] == before["resource_operation_id"]


@pytest.mark.parametrize("mutation", [
    {"submitted": True}, {"status": "pending", "submission_protocol": None},
    {"resource_operation_id": "another-operation"}, {"packet_sha256": "stale"},
    {"model": "another-model"}, {"status": "operational_failure", "failed_at": None},
    {"transport": "codex"}, {"estimate": {}},
])
def test_unproven_preflight_never_starts_a_model(run, monkeypatch, mutation):
    fail_copy_once(monkeypatch)
    client = Fake()
    with pytest.raises(ar.AstraRetryableError):
        ar.review_run(run, budget_usd=100, client=client)
    path = run / ar.RECEIPT_FILE
    pending = json.loads(path.read_text())
    pending.update(mutation)
    path.write_text(json.dumps(pending))
    with pytest.raises(ar.AstraReviewError):
        ar.review_run(run, budget_usd=100, client=client)
    assert client.calls == []


def test_locked_new_preflight_can_resume_after_process_interruption(run, monkeypatch):
    fail_copy_once(monkeypatch, KeyboardInterrupt())
    client = Fake()
    with pytest.raises(KeyboardInterrupt):
        ar.review_run(run, budget_usd=100, client=client)
    saved = json.loads((run / ar.RECEIPT_FILE).read_text())
    assert saved["status"] == "pending" and saved["submitted"] is False
    assert ar.review_run(run, budget_usd=100, client=client)["delivery_ready"]
    assert len(client.calls) == 1


def test_legacy_terminal_unsubmitted_failure_can_resume(run, monkeypatch):
    fail_copy_once(monkeypatch)
    client = Fake()
    with pytest.raises(ar.AstraRetryableError):
        ar.review_run(run, budget_usd=100, client=client)
    path = run / ar.RECEIPT_FILE
    pending = json.loads(path.read_text())
    pending.pop("submission_protocol")
    path.write_text(json.dumps(pending))
    assert ar.review_run(run, budget_usd=100, client=client)["delivery_ready"]
    assert len(client.calls) == 1


def test_live_preflight_owner_prevents_second_submission(run, monkeypatch):
    original = ar.shutil.copyfile
    client = Fake()
    def copy(*args, **kwargs):
        with pytest.raises(ar.AstraReviewError, match="Another API Astra"):
            ar.review_run(run, budget_usd=100, client=client)
        return original(*args, **kwargs)
    monkeypatch.setattr(ar.shutil, "copyfile", copy)
    assert ar.review_run(run, budget_usd=100, client=client)["delivery_ready"]
    assert len(client.calls) == 1


def test_saved_response_transport_is_explicitly_retryable_without_post(run, monkeypatch):
    class Background(Fake):
        gets = 0
        def create(self, **kwargs):
            self.completed = super().create(**kwargs)
            return {"id": self.completed["id"], "status": "in_progress", "output": []}
        def retrieve(self, response_id):
            self.gets += 1
            if self.gets <= 2:
                raise ConnectionError("connection reset")
            assert response_id == self.completed["id"]
            return self.completed
    monkeypatch.setattr(ar.time, "sleep", lambda _: None)
    client = Background()
    for _ in range(2):
        with pytest.raises(ar.AstraRetryableError):
            ar.review_run(run, budget_usd=100, client=client)
    assert ar.review_run(run, budget_usd=100, client=client)["delivery_ready"]
    assert len(client.calls) == 1 and client.gets == 3


def test_unsubmitted_changed_evidence_or_lower_allowance_cannot_resume(run, monkeypatch):
    fail_copy_once(monkeypatch)
    client = Fake()
    with pytest.raises(ar.AstraRetryableError):
        ar.review_run(run, budget_usd=100, max_output_tokens=300, client=client)
    with pytest.raises(ar.AstraReviewError, match="output allowance"):
        ar.review_run(run, budget_usd=100, max_output_tokens=200, client=client)
    (run / "finished_walk.json").write_text('{"residuals": [{"problem": "changed"}]}')
    with pytest.raises(ar.AstraReviewError, match="Evidence changed"):
        ar.review_run(run, budget_usd=100, client=client)
    assert client.calls == []
