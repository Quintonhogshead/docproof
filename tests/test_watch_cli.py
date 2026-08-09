"""The commands, and what they say when something is missing.

Half of this is exit codes, because the thing running `once` four times a day
is launchd and not a person: 0 means the run happened, 2 means somebody has to
set something up before it ever can, and 3 means it ran and some of it did not
work. The other half is that every refusal names the command that fixes it —
whoever reads these messages is reading them in a log file, hours later, with
no idea what the watcher was thinking.
"""
from __future__ import annotations

import json
import plistlib
import subprocess

import pytest

from app.lock import FolderLock
from app.watch import cli
from app.watch import schedule as schedulelib
from app.watch import tick as ticklib
from app.watch.settings import WatchSettings
from app.watch.stages import FORMATTED, STATE_PROP
from app.watch.state import FileRecord, WatchState

from .conftest import FIXTURES
from .fakes import TaggingProvider, drive_entry, fake_drive

FOLDER = "1AbCdEfGhIjKlMnOp"
MANUSCRIPT = (FIXTURES / "googledoc.docx").read_bytes()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A watch home nobody else knows about, and no ambient sign-in.

    Both readers are stubbed: the CLI's own, and the one the preflight in `tick`
    reaches through — otherwise a real Google token in the developer's Keychain
    leaks in and a "not signed in" test tries to reach the network."""
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("app.watch.cli.get_api_key", lambda name: None)
    monkeypatch.setattr("app.watch.tick.get_api_key", lambda name: None)
    return tmp_path


def configured(home, **over) -> WatchSettings:
    fields = {"folder_id": FOLDER, "model": "claude-haiku-4-5",
              "client_id": "client-1", "client_secret": "secret-1", **over}
    ws = WatchSettings(**fields)
    ws.save(home)
    return ws


def signed_in(monkeypatch, token="refresh-1"):
    monkeypatch.setattr("app.watch.cli.get_api_key", lambda name: token)
    monkeypatch.setattr("app.watch.tick.get_api_key", lambda name: token)


def run(home, *argv) -> int:
    return cli.main(["--home", str(home), *argv])


def drive(monkeypatch, opener):
    """Every Drive call this run makes goes through the fake."""
    monkeypatch.setattr("app.watch.drive._open_url", opener)
    monkeypatch.setattr("app.watch.tick.drive._open_url", opener)
    real_tick = ticklib.tick
    monkeypatch.setattr("app.watch.cli.ticklib.tick",
                        lambda h, ws, **kw: real_tick(h, ws, opener=opener, **kw))


# --- before anything is set up ------------------------------------------------

def test_a_pass_with_nothing_set_up_says_what_to_run(home, capsys):
    assert run(home, "once") == cli.UNUSABLE
    assert "docproof-watch init" in capsys.readouterr().err


def test_a_pass_with_no_sign_in_says_what_to_run(home, capsys, monkeypatch):
    configured(home)

    assert run(home, "once") == cli.UNUSABLE
    assert "docproof-watch auth" in capsys.readouterr().err


def test_status_with_nothing_set_up_still_answers(home, capsys):
    assert run(home, "status") == cli.OK
    out = capsys.readouterr().out
    assert "not set yet" in out
    assert "Nothing has been prepared yet." in out


def test_auth_status_says_it_is_not_signed_in(home, capsys):
    assert run(home, "auth", "--status") == cli.UNUSABLE
    assert "Not signed in" in capsys.readouterr().out


def test_auth_status_says_where_a_sign_in_came_from(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)

    assert run(home, "auth", "--status") == cli.OK
    assert "Keychain" in capsys.readouterr().out


def test_auth_status_says_when_there_is_no_oauth_client(home, capsys,
                                                        monkeypatch):
    signed_in(monkeypatch)

    run(home, "auth", "--status")

    assert "docs/watch.md" in capsys.readouterr().out


# --- saying what to watch -----------------------------------------------------

def test_init_takes_the_address_out_of_the_bar(home, capsys):
    run(home, "init", "--folder",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER}?usp=sharing")

    assert WatchSettings.load(home).folder_id == FOLDER
    assert FOLDER in capsys.readouterr().out


def test_init_refuses_something_that_is_not_a_folder(home, capsys):
    assert run(home, "init", "--folder", "my documents") == cli.UNUSABLE
    assert "Google Drive folder" in capsys.readouterr().err


def test_init_refuses_a_model_that_does_not_exist(home, capsys):
    assert run(home, "init", "--model", "gpt-9") == cli.UNUSABLE
    err = capsys.readouterr().err
    assert "not a model DocProof knows" in err
    assert "claude-sonnet-5" in err          # and says what would work


def test_init_says_what_is_still_missing(home, capsys):
    assert run(home, "init", "--folder", FOLDER) == cli.UNUSABLE
    assert "docproof-watch auth" in capsys.readouterr().out


def test_init_says_it_is_ready_when_it_is(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)

    assert run(home, "init", "--model", "claude-opus-5") == cli.OK
    out = capsys.readouterr().out
    assert "Ready" in out and "--dry-run" in out
    assert WatchSettings.load(home).model == "claude-opus-5"


def test_init_keeps_what_it_was_not_asked_to_change(home):
    configured(home, model="claude-opus-5")

    run(home, "init", "--output", "both")

    ws = WatchSettings.load(home)
    assert ws.model == "claude-opus-5" and ws.prep_output == "both"
    assert ws.client_id == "client-1"


# --- a pass -------------------------------------------------------------------

def test_a_dry_run_says_what_it_would_do_and_does_nothing(home, capsys,
                                                          monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    opener = fake_drive({"f-1": drive_entry("Wolves.docx")}, docx=MANUSCRIPT)
    drive(monkeypatch, opener)

    assert run(home, "once", "--dry-run") == cli.OK
    out = capsys.readouterr().out
    assert "to prepare" in out and "Wolves.docx" in out
    assert "Nothing was downloaded" in out
    assert not (home / "jobs").exists()


def test_a_pass_prepares_what_is_there_and_says_so(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    monkeypatch.setattr("app.jobs.build_provider",
                        lambda cfg, api_key=None: TaggingProvider())
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    opener = fake_drive({"f-1": drive_entry("Wolves.docx")}, docx=MANUSCRIPT)
    drive(monkeypatch, opener)

    assert run(home, "once") == cli.OK

    out = capsys.readouterr().out
    assert "Prepared 1: Wolves.docx" in out
    assert "Wolves - book 0.docx" in out
    assert opener.files["f-1"]["appProperties"][STATE_PROP] == FORMATTED


def test_an_empty_folder_is_not_a_problem(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    drive(monkeypatch, fake_drive())

    assert run(home, "once") == cli.OK
    assert "Nothing new in the folder" in capsys.readouterr().out


def test_a_pass_where_something_failed_exits_three(home, capsys, monkeypatch):
    """Distinguishable from "could not start" on purpose: whoever reads the
    log needs to know whether the run happened."""
    configured(home)
    signed_in(monkeypatch)
    monkeypatch.setattr(
        "app.jobs.build_provider",
        lambda cfg, api_key=None: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr("app.jobs.get_api_key", lambda p: "test-key")
    opener = fake_drive({"f-1": drive_entry("Wolves.docx")}, docx=MANUSCRIPT)
    drive(monkeypatch, opener)

    assert run(home, "once") == cli.PARTIAL
    assert "Could not prepare Wolves.docx" in capsys.readouterr().err


def test_a_run_that_overran_its_schedule_is_skipped_politely(home, capsys,
                                                             monkeypatch):
    """A long book can outlast the gap between two scheduled runs. The right
    answer is to let the first finish — and to not tell launchd anything went
    wrong, because nothing did."""
    configured(home)
    signed_in(monkeypatch)
    drive(monkeypatch, fake_drive())

    with FolderLock(home):
        code = run(home, "once")

    assert code == cli.OK
    assert "still working on this folder" in capsys.readouterr().out


def test_a_dry_run_claims_nothing_so_it_never_collides(home, capsys,
                                                       monkeypatch):
    """It starts no runner and writes no job, so there is nothing for a
    second copy to collide with — and asking somebody to wait to look is
    silly."""
    configured(home)
    signed_in(monkeypatch)
    drive(monkeypatch, fake_drive({"f-1": drive_entry("Wolves.docx")}))

    with FolderLock(home):
        assert run(home, "once", "--dry-run") == cli.OK

    assert "would prepare 1" in capsys.readouterr().out


def test_a_revoked_sign_in_stops_the_run_and_says_to_sign_in_again(
        home, capsys, monkeypatch):
    from .fakes import http_error
    configured(home)
    signed_in(monkeypatch)
    drive(monkeypatch, fake_drive(fail={"token": http_error(400,
                                                            "invalid_grant")}))

    assert run(home, "once") == cli.UNUSABLE
    assert "docproof-watch auth" in capsys.readouterr().err


# --- what it has been doing ---------------------------------------------------

def test_status_lists_what_has_been_prepared(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    state = WatchState(home / "state.json")
    state.record(FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1",
                            marked=FORMATTED,
                            uploaded={"tagged_Wolves.docx": "up-1"}))

    assert run(home, "status") == cli.OK

    out = capsys.readouterr().out
    assert "Wolves.docx" in out
    assert "formatted" in out
    assert "tagged_Wolves.docx" in out


def test_status_does_not_need_the_folder_lock(home, capsys, monkeypatch):
    configured(home)
    signed_in(monkeypatch)

    with FolderLock(home):
        assert run(home, "status") == cli.OK

    assert FOLDER in capsys.readouterr().out


# --- handing it to macOS ------------------------------------------------------

@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A launch agent somewhere harmless, and a launchctl that only records.

    Nothing in this suite may write to ~/Library/LaunchAgents or load anything
    into the session running it."""
    path = tmp_path / "agent.plist"

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.watch.schedule.plist_path", lambda: path)
    monkeypatch.setattr("app.watch.schedule.subprocess.run", run)
    monkeypatch.setattr("app.watch.schedule.executable",
                        lambda: "/usr/local/bin/docproof-watch")
    return path


def test_scheduling_says_when_it_will_run_and_what_it_wrote(home, capsys,
                                                            agent, monkeypatch):
    configured(home)
    signed_in(monkeypatch)

    assert run(home, "schedule") == cli.OK

    out = capsys.readouterr().out
    assert "06:00, 11:00, 16:00, 21:00" in out
    assert str(agent) in out
    assert plistlib.loads(agent.read_bytes())["Label"] == schedulelib.LABEL


def test_scheduling_says_a_sleeping_mac_runs_nothing(home, capsys, agent,
                                                     monkeypatch):
    """Said plainly rather than discovered in three weeks."""
    configured(home)
    signed_in(monkeypatch)

    run(home, "schedule")

    out = capsys.readouterr().out
    assert "sleeping Mac runs nothing" in out
    assert "not made up later" in out


def test_the_times_can_be_chosen(home, capsys, agent, monkeypatch):
    configured(home)
    signed_in(monkeypatch)

    assert run(home, "schedule", "--times", "07:30,19:00") == cli.OK

    entries = plistlib.loads(agent.read_bytes())["StartCalendarInterval"]
    assert entries == [{"Hour": 7, "Minute": 30}, {"Hour": 19, "Minute": 0}]


def test_scheduling_before_signing_in_works_but_says_so(home, capsys, agent):
    """Nothing stops somebody setting the schedule up first. It would just be
    a surprise to find out in three weeks that nothing had happened."""
    configured(home)

    assert run(home, "schedule") == cli.OK
    assert "nothing will happen until" in capsys.readouterr().out
    assert agent.exists()


def test_a_schedule_of_impossible_times_is_refused(home, capsys, agent):
    configured(home)

    assert run(home, "schedule", "--times", "25:00") == cli.UNUSABLE
    assert "no such time" in capsys.readouterr().err
    assert not agent.exists()


def test_unscheduling_says_it_stopped(home, capsys, agent, monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    run(home, "schedule")
    capsys.readouterr()

    assert run(home, "unschedule") == cli.OK

    assert "not look in the folder on its own" in capsys.readouterr().out
    assert not agent.exists()


def test_unscheduling_when_there_was_no_schedule_is_not_a_failure(home, capsys,
                                                                  agent):
    assert run(home, "unschedule") == cli.OK
    assert "no schedule to remove" in capsys.readouterr().out


def test_status_says_when_it_will_next_look(home, capsys, agent, monkeypatch):
    configured(home)
    signed_in(monkeypatch)
    run(home, "schedule", "--times", "06:00")
    capsys.readouterr()

    run(home, "status")

    assert "Runs at: 06:00" in capsys.readouterr().out


def test_status_says_when_nothing_is_scheduled(home, capsys, agent):
    configured(home)

    run(home, "status")

    assert "only when you say so" in capsys.readouterr().out


# --- the home -----------------------------------------------------------------

def test_the_home_can_be_put_somewhere_else(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("app.watch.cli.get_api_key", lambda name: None)
    elsewhere = tmp_path / "elsewhere"

    run(elsewhere, "init", "--folder", FOLDER)

    assert json.loads((elsewhere / "watch.json").read_text())["folder_id"] == \
        FOLDER


def test_the_bundle_does_one_pass_and_never_opens_a_window(monkeypatch):
    """What macOS runs four times a day on a Mac that only ever had the .app.
    It must reach the watcher's own command line, and must not reach the part
    that builds a window."""
    import docproof_desktop
    seen = []
    monkeypatch.setattr("app.watch.cli.main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(
        "app.desktop.main",
        lambda argv=None: pytest.fail("a scheduled pass opened the app"))

    code = docproof_desktop.main(["--home", "/tmp/w", "--watch-once"])

    assert code == 0
    assert seen == [["--home", "/tmp/w", "once"]]


def test_the_bundle_opens_the_app_when_it_is_not_a_scheduled_pass(monkeypatch):
    import docproof_desktop
    opened = []
    monkeypatch.setattr("app.desktop.main",
                        lambda argv=None: opened.append(argv) or 0)

    assert docproof_desktop.main([]) == 0
    assert opened == [[]]


def test_a_scheduled_run_leaves_a_log_behind(home, capsys, monkeypatch):
    """The only place anybody can look afterwards to see why a morning was
    quiet."""
    configured(home)
    signed_in(monkeypatch)
    drive(monkeypatch, fake_drive())

    run(home, "once")

    assert (home / cli.LOG_FILE).is_file()
