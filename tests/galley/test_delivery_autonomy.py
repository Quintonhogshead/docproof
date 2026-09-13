"""Retain completed work through an outage without reviving stale requests."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import docx
import pytest

from galley import agent as ga
from tests.galley.test_agent import BOOK, ENV_TEXT, _agent


@pytest.fixture()
def agent(tmp_path):
    path = tmp_path / "agent.env"
    path.write_text(ENV_TEXT)
    path.chmod(0o600)
    return _agent(ga.read_env(path), tmp_path)


def _abandoned(agent, monkeypatch):
    from galley import astra_review

    book = ga.AwaitingBook.from_json(BOOK)
    ws = agent.root / "test-drive-1"
    run = ws / "runs/final"
    run.mkdir(parents=True)
    manuscript = run / "book - Atmosphere Press Proofreader.docx"
    document = docx.Document()
    document.add_paragraph("Reviewed manuscript.")
    document.save(manuscript)
    artifacts = [manuscript]
    handoff = ws / "handoff"
    handoff.mkdir()
    for number in range(7):
        path = handoff / f"evidence-{number}.md"
        path.write_text("Frozen delivery evidence.")
        artifacts.append(path)
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    package = {"run": str(run), "source_id": book.file_id,
               "packet_sha256": "packet", "build_sha256": sha(manuscript),
               "artifacts": [{"path": str(p), "name": p.name, "sha256": sha(p)}
                             for p in artifacts]}
    path = ws / "runs/driver/package.json"
    path.parent.mkdir()
    path.write_text(json.dumps(package))
    monkeypatch.setattr(astra_review, "validate_receipt", lambda run: {
        "delivery_ready": True, "packet_sha256": "packet",
        "review": {"editorial_verdict": "ready"}})
    ledger = agent.ledger()
    ledger.record(book.file_id, ga.FAILED, delivery="abandoned",
                  verified_publication=True, name=book.name, slug=ws.name,
                  request_id="", folder_id=book.folder_id, outcome="done",
                  handoff_files=[str(p) for p in artifacts], delivery_attempts=6)
    return book, ledger, artifacts, path


def test_same_current_request_with_validated_package_resumes_delivery(agent, monkeypatch):
    book, ledger, _, _ = _abandoned(agent, monkeypatch)
    agent._resume_abandoned_delivery(book, ledger)
    entry = ledger.claimed(book.file_id)
    assert entry["state"] == ga.PENDING_DELIVERY
    assert entry["next_delivery_at"] == 0
    assert entry["delivery_attempts"] == 6
    assert entry["delivery_retry_alerted"] is True


@pytest.mark.parametrize("change", ["new_request", "new_folder", "unverified",
                                   "changed_file", "deleted_file", "other_source"])
def test_stale_or_changed_abandoned_packages_are_not_reactivated(agent, monkeypatch, change):
    book, ledger, artifacts, package_path = _abandoned(agent, monkeypatch)
    if change == "new_request":
        book = replace(book, request_id="new-request")
    elif change == "new_folder":
        book = replace(book, folder_id="other-folder")
    elif change == "unverified":
        ledger.record(book.file_id, ga.FAILED, verified_publication=False)
    elif change == "changed_file":
        artifacts[-1].write_text("Changed after review.")
    elif change == "deleted_file":
        artifacts[-1].unlink()
    else:
        package = json.loads(package_path.read_text())
        package["source_id"] = "another-book"
        package_path.write_text(json.dumps(package))
    agent._resume_abandoned_delivery(book, ledger)
    assert ledger.state(book.file_id) == ga.FAILED
    assert ledger.claimed(book.file_id)["delivery"] == "abandoned"


def test_invalid_review_does_not_break_polling_or_reactivate_package(agent, monkeypatch):
    from galley import astra_review

    book, ledger, _, _ = _abandoned(agent, monkeypatch)
    def invalid(run):
        raise astra_review.AstraReviewError("Review no longer covers the book")
    monkeypatch.setattr(astra_review, "validate_receipt", invalid)
    agent._resume_abandoned_delivery(book, ledger)
    assert ledger.state(book.file_id) == ga.FAILED


def test_backoff_remains_finite_after_arbitrarily_long_outage(agent):
    assert agent._delivery_wait(1) == agent.poll_interval_s
    assert agent._delivery_wait(10**6) == ga.MAX_DELIVERY_BACKOFF_S
