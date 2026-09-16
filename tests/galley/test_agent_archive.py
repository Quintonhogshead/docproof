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
