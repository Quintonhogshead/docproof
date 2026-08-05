"""What the watcher has been doing, as both front ends read it.

The terminal prints this and the panel draws it, so the point of these tests is
that there is one answer rather than two that drift.
"""
from __future__ import annotations

import pytest

from app.jobs import Job, JobStore
from app.settings import Paths
from app.watch import cli
from app.watch.settings import WatchSettings
from app.watch.state import FileRecord, WatchState
from app.watch.status import PLAIN_STAGE, missing, status

SIGNED = lambda name: "refresh-1"        # noqa: E731 - a stand-in keychain
UNSIGNED = lambda name: None             # noqa: E731


def configured(**over) -> WatchSettings:
    fields = {"folder_id": "1AbCdEfGhIjKlMnOp", "client_id": "c",
              "client_secret": "s", **over}
    return WatchSettings(**fields)


# --- what is still needed -----------------------------------------------------

def test_nothing_set_up_reads_as_needing_a_folder():
    assert missing(WatchSettings(), get_key=UNSIGNED) == "folder"


def test_a_folder_with_no_sign_in_reads_as_needing_one():
    assert missing(configured(), get_key=UNSIGNED) == "auth"


def test_a_folder_with_no_oauth_client_reads_as_needing_a_sign_in():
    assert missing(configured(client_id=""), get_key=SIGNED) == "auth"


def test_a_watcher_that_is_ready_needs_nothing():
    assert missing(configured(), get_key=SIGNED) is None


def test_it_answers_a_word_rather_than_a_sentence():
    """The terminal names a command and the panel names a card. Neither should
    be reading the other's prose to find out what is wrong."""
    assert missing(WatchSettings(), get_key=UNSIGNED) == "folder"
    assert "docproof-watch" not in (missing(WatchSettings(),
                                            get_key=UNSIGNED) or "")


# --- reading it is free -------------------------------------------------------

def test_reading_the_status_does_not_create_the_watch_home(tmp_path):
    """The panel asks this every five seconds. A question must not make
    folders — least of all in a DocProof nobody has pointed at a Drive."""
    home = tmp_path / "never-used"

    answer = status(home, get_key=UNSIGNED)

    assert answer["files"] == []
    assert answer["missing"] == "folder"
    assert not home.exists()


def test_a_watcher_with_nothing_done_yet_still_answers(tmp_path):
    configured().save(tmp_path)

    answer = status(tmp_path, get_key=SIGNED)

    assert answer["folder_id"] == "1AbCdEfGhIjKlMnOp"
    assert answer["signed_in"] is True
    assert answer["files"] == []
    assert answer["last_tick_at"] is None


# --- the manuscripts ----------------------------------------------------------

def seen(tmp_path, *records) -> None:
    state = WatchState(tmp_path / "state.json")
    for rec in records:
        state.record(rec)


def test_the_manuscripts_are_newest_first(tmp_path):
    seen(tmp_path,
         FileRecord(file_id="f-1", name="First.docx"),
         FileRecord(file_id="f-2", name="Second.docx"),
         FileRecord(file_id="f-3", name="Third.docx"))

    names = [row["name"] for row in status(tmp_path, get_key=SIGNED)["files"]]

    assert names == ["Third.docx", "Second.docx", "First.docx"]


def test_a_manuscripts_cost_comes_off_its_job(tmp_path):
    JobStore(Paths(tmp_path)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="done",
            cost=1.23, words=90_000))
    seen(tmp_path, FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1",
                              marked="formatted",
                              uploaded={"tagged_Wolves.docx": "up-1"}))

    row = status(tmp_path, get_key=SIGNED)["files"][0]

    assert row["cost"] == 1.23
    assert row["words"] == 90_000
    assert row["said"] == "formatted"
    assert row["plain_state"] == "Prepared"
    assert row["uploaded"] == ["tagged_Wolves.docx"]


def test_a_manuscript_with_no_job_yet_is_being_looked_at(tmp_path):
    seen(tmp_path, FileRecord(file_id="f-1", name="Wolves.docx"))

    row = status(tmp_path, get_key=SIGNED)["files"][0]

    assert row["said"] == "in progress"
    assert row["cost"] is None


def test_a_manuscript_being_prepared_says_how_far_along(tmp_path):
    JobStore(Paths(tmp_path)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="running",
            done=3, total=8))
    seen(tmp_path, FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1"))

    row = status(tmp_path, get_key=SIGNED)["files"][0]

    assert row["plain_state"] == "Reading your manuscript (3 of 8)"
    assert (row["done"], row["total"]) == (3, 8)


def test_a_manuscript_that_failed_says_so_and_keeps_the_reason(tmp_path):
    JobStore(Paths(tmp_path)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="failed",
            error="The finished file did not match the manuscript.",
            verified=False))
    seen(tmp_path, FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1",
                              marked="failed"))

    row = status(tmp_path, get_key=SIGNED)["files"][0]

    assert row["plain_state"] == "Needs attention"
    assert "did not match" in row["error"]


# --- one vocabulary -----------------------------------------------------------

def test_the_labels_the_terminal_prints_are_the_labels_the_panel_shows():
    """One copy. The two saying different words about the same file is how a
    support conversation goes wrong."""
    assert cli._PLAIN is PLAIN_STAGE
    assert PLAIN_STAGE["new"] == "to prepare"


def test_the_terminal_and_the_library_agree_about_one_manuscript(tmp_path,
                                                                 capsys,
                                                                 monkeypatch):
    monkeypatch.setattr("app.watch.cli.get_api_key", SIGNED)
    configured().save(tmp_path)
    JobStore(Paths(tmp_path)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="done",
            cost=0.41))
    seen(tmp_path, FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1",
                              marked="formatted",
                              uploaded={"tagged_Wolves.docx": "up-1"}))

    cli.main(["--home", str(tmp_path), "status"])
    printed = capsys.readouterr().out
    row = status(tmp_path, get_key=SIGNED)["files"][0]

    assert row["name"] in printed
    assert row["said"] in printed
    assert f"${row['cost']:.2f}" in printed
    assert row["uploaded"][0] in printed
