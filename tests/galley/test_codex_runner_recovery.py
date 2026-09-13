"""Recover transport bookkeeping without fresh allowances or duplicate work."""
from __future__ import annotations

import json

import pytest

from galley import codex_runner as cr
from galley.astra_review import AstraReviewError
from tests.galley.test_codex_runner import RESULT, fake, receipt, run  # noqa: F401


def test_failed_attempt_artifacts_are_archived_before_retry(fake, tmp_path):
    fake.output = "incomplete response"
    with pytest.raises(cr.CodexRetryableError):
        run(tmp_path)
    old = receipt(tmp_path)
    fake.output = json.dumps(RESULT)
    run(tmp_path)
    current = receipt(tmp_path)
    archive = cr.Path(current["retry_authorization"]["archive"])
    assert json.loads((archive / "receipt.json").read_text()) == old
    assert (archive / "final.json").read_text() == "incomplete response"
    assert current["execution_budget"]["elapsed_seconds"] >= old["execution_budget"]["elapsed_seconds"]
    assert current["execution_budget"]["timeout_seconds"] == old["execution_budget"]["timeout_seconds"]


def test_repeated_invalid_output_exhausts_saved_attempt_limit(fake, tmp_path):
    fake.output = "{}"
    for attempt in range(1, cr.MAX_AUTOMATIC_ATTEMPTS + 1):
        with pytest.raises(AstraReviewError) as error:
            run(tmp_path)
        assert error.value.retryable is (attempt < cr.MAX_AUTOMATIC_ATTEMPTS)
        assert receipt(tmp_path)["attempt"] == attempt
    calls = len(fake.calls)
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == calls


def test_retry_cannot_expand_original_timeout(fake, tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(cr.time, "monotonic", lambda: now[0])
    original = cr._execute
    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 1 if kwargs["prompt"] is None else 4
        return result
    monkeypatch.setattr(cr, "_execute", execute)
    fake.output = "{}"
    with pytest.raises(cr.CodexRetryableError):
        run(tmp_path, timeout_seconds=12)
    assert receipt(tmp_path)["execution_budget"]["elapsed_seconds"] == 5
    fake.output = json.dumps(RESULT)
    assert run(tmp_path, timeout_seconds=120) == RESULT
    generation = [c for c in fake.calls if not c.is_auth][-1]
    assert generation.timeout == 6  # 12 total minus 5 used minus the next login check.
    assert receipt(tmp_path)["execution_budget"]["timeout_seconds"] == 12


@pytest.mark.parametrize("damage", ["legacy_running", "no_exit_proof", "changed_output"])
def test_unprovable_or_changed_completed_output_is_never_adopted(fake, tmp_path, damage):
    run(tmp_path)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    saved = receipt(tmp_path)
    saved["status"] = "running"
    if damage == "legacy_running":
        for key in ("process_exited", "exit_code", "execution_budget", "result_sha256", "output_sha256"):
            saved.pop(key, None)
    elif damage == "no_exit_proof":
        saved["process_exited"] = False
    else:
        (directory / "result.json").write_text(json.dumps({**RESULT, "ready": False}))
    (directory / "receipt.json").write_text(json.dumps(saved))
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_completed_output_can_close_crash_before_result_json(fake, tmp_path):
    run(tmp_path)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    saved = receipt(tmp_path)
    saved["status"] = "running"
    saved.pop("result_sha256")
    (directory / "result.json").unlink()
    (directory / "receipt.json").write_text(json.dumps(saved))
    assert run(tmp_path) == RESULT
    assert len(fake.calls) == 2


def test_legacy_failed_request_without_original_budget_is_not_replayed(fake, tmp_path):
    fake.output = "{}"
    with pytest.raises(cr.CodexRetryableError):
        run(tmp_path)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    saved = receipt(tmp_path)
    saved.pop("execution_budget")
    (directory / "receipt.json").write_text(json.dumps(saved))
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_preflight_only_orphan_output_is_preserved_without_blocking(fake, tmp_path):
    fake.auth_returncode = 1
    with pytest.raises(AstraReviewError, match="subscription login"):
        run(tmp_path)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    (directory / "final.json").write_text("Old unsubmitted output")
    fake.auth_returncode = 0
    assert run(tmp_path) == RESULT
    assert [p.read_text() for p in (directory / "preflight-output").iterdir()] == ["Old unsubmitted output"]


def test_output_without_any_submission_receipt_does_not_authorize_new_work(fake, tmp_path):
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    directory.mkdir(parents=True)
    (directory / "final.json").write_text(json.dumps(RESULT))
    with pytest.raises(AstraReviewError, match="without a submission receipt"):
        run(tmp_path)
    assert fake.calls == []


def test_user_cancellation_is_not_a_retryable_transport_failure(fake, tmp_path):
    fake.returncode = 130
    with pytest.raises(AstraReviewError, match="cancelled") as failure:
        run(tmp_path)
    assert failure.value.retryable is False
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2
