"""The agent carries DocWatch's archive folder from the awaiting list to the
driver, remembers it on the claim, and refreshes it when DocWatch names one
later — so a delivery pending for want of an archive can finish."""
from __future__ import annotations

from galley import agent as ga

from .test_agent import BOOK, FakeApp, FakeResult, _agent, _downloader, env, env_file  # noqa: F401

ARCHIVED = {**BOOK, "archive_folder_id": "archive-root"}


def test_awaiting_book_reads_the_archive_folder():
    assert ga.AwaitingBook.from_json(ARCHIVED).archive_folder_id == "archive-root"
    assert ga.AwaitingBook.from_json(BOOK).archive_folder_id == ""


def test_the_driver_is_told_where_to_file_the_record(env, tmp_path):
    ran: list[dict] = []
    agent = _agent(env, tmp_path, opener=FakeApp([ARCHIVED]), download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    assert ran[0]["drive_folder_id"] == "folder-A"
    assert ran[0]["drive_archive_folder_id"] == "archive-root"
    assert agent.ledger().claimed("drive-1")["archive_folder_id"] == "archive-root"


def test_no_archive_means_no_archive_argument(env, tmp_path):
    ran: list[dict] = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]), download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    assert "drive_archive_folder_id" not in ran[0]


def test_the_rehearsal_override_wins(env, tmp_path):
    ran: list[dict] = []
    agent = _agent(env, tmp_path, opener=FakeApp([ARCHIVED]), download=_downloader(tmp_path),
                   drive_archive_override="my-test-archive",
                   run_driver=lambda **kw: ran.append(kw) or FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    assert ran[0]["drive_archive_folder_id"] == "my-test-archive"


def test_a_claimed_book_picks_up_an_archive_named_later(env, tmp_path):
    """Claimed with no archive; DocWatch configures one; the next poll records it
    on the claim so the pending delivery retry can file the record."""
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]), download=_downloader(tmp_path),
                   run_driver=lambda **kw: FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    ledger = agent.ledger()
    assert ledger.claimed("drive-1")["archive_folder_id"] == ""
    agent.opener = FakeApp([ARCHIVED])
    agent.poll_once()
    assert agent.ledger().claimed("drive-1")["archive_folder_id"] == "archive-root"


def test_a_reset_book_with_an_archive_is_claimed_afresh_not_crashed(env, tmp_path):
    """2026-09-16: a proof-flag reset in DocWatch gives the book a new
    request id. The poll replaces its ledger entry with one that has no
    state yet, and the archive-folder refresh then read entry["state"] and
    crashed every poll. The fresh entry is claimed below and takes the
    archive folder there."""
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([ARCHIVED]), download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    assert len(ran) == 1
    agent.opener = FakeApp([{**ARCHIVED, "request_id": "2026-09-16T14:35:00Z"}])
    agent.poll_once()
    assert len(ran) == 2 and ran[1]["drive_archive_folder_id"] == "archive-root"
    entry = agent.ledger().claimed("drive-1")
    assert entry["request_id"] == "2026-09-16T14:35:00Z"
    assert entry["state"] == ga.FINISHED and entry["archive_folder_id"] == "archive-root"


def test_a_forgotten_book_re_requested_with_an_archive_is_claimed(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([{**ARCHIVED, "request_id": "2026-09-16T14:35:00Z"}]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult(uploaded=["up-1"]))
    agent.poll_once()
    assert len(ran) == 1 and ran[0]["drive_archive_folder_id"] == "archive-root"
    assert agent.ledger().claimed("drive-1")["state"] == ga.FINISHED
