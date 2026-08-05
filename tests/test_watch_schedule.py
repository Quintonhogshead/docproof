"""Handing the watcher to launchd.

Nothing here runs `launchctl` — the command runner is injected the way
app/update.py injects one, so the tests read what would have been run instead
of loading a real agent into the session running them. The plist itself is
read back with `plistlib`, because the thing that matters is what launchd
would understand, not what the file looks like.
"""
from __future__ import annotations

import plistlib
import subprocess

import pytest

from app.watch import schedule as schedulelib
from app.watch.schedule import LABEL, ScheduleError


# Held before the fixture below replaces it, for the two tests that are about
# finding the command rather than about scheduling one.
REAL_EXECUTABLE = schedulelib.executable
COMMAND = "/usr/local/bin/docproof-watch"


@pytest.fixture(autouse=True)
def a_command_to_schedule(monkeypatch):
    """Which `docproof-watch` this machine has is not what most of these are
    asking about, and under pytest there may not be one on the PATH at all."""
    monkeypatch.setattr(schedulelib, "executable", lambda: COMMAND)


def recorder(*, fails: set[str] | None = None):
    """Stands in for subprocess.run, recording what launchctl was asked."""
    calls = []
    failing = fails or set()

    def run(command, **kwargs):
        calls.append(command)
        code = 1 if command[1] in failing else 0
        return subprocess.CompletedProcess(command, code, stdout="",
                                           stderr="Bootstrap failed: 5: I/O")

    run.calls = calls
    return run


@pytest.fixture
def plist(tmp_path):
    return tmp_path / "agent.plist"


def written(path):
    return plistlib.loads(path.read_bytes())


# --- when to run --------------------------------------------------------------

def test_times_are_read_the_way_people_write_them():
    assert schedulelib.parse_times("06:00,11:00,16:00,21:00") == [
        (6, 0), (11, 0), (16, 0), (21, 0)]


def test_times_are_tidied_rather_than_taken_literally():
    """Spaces, duplicates and a careless order are all things somebody types."""
    assert schedulelib.parse_times(" 21:00, 06:00 ,06:00 ") == [(6, 0), (21, 0)]


@pytest.mark.parametrize("junk", ["25:00", "06:99", "-1:00"])
def test_a_time_that_does_not_exist_is_refused(junk):
    with pytest.raises(ScheduleError, match="no such time"):
        schedulelib.parse_times(junk)


@pytest.mark.parametrize("junk", ["morning", "6", "06:ish"])
def test_something_that_is_not_a_time_says_how_to_write_one(junk):
    with pytest.raises(ScheduleError, match="HH:MM"):
        schedulelib.parse_times(junk)


def test_no_times_at_all_is_refused():
    with pytest.raises(ScheduleError, match="No times"):
        schedulelib.parse_times(" , ")


# --- what launchd is given ----------------------------------------------------

def test_the_agent_carries_one_entry_per_time(tmp_path, plist):
    schedulelib.install(schedulelib.parse_times("06:00,11:00,16:00,21:00"),
                        tmp_path, run=recorder(), path=plist)

    entries = written(plist)["StartCalendarInterval"]

    assert entries == [{"Hour": 6, "Minute": 0}, {"Hour": 11, "Minute": 0},
                       {"Hour": 16, "Minute": 0}, {"Hour": 21, "Minute": 0}]
    assert written(plist)["Label"] == LABEL


def test_installing_a_schedule_does_not_start_a_run(tmp_path, plist):
    """Setting up a schedule should not immediately spend money. The first run
    is at the first scheduled time; anybody wanting one now types `once`."""
    schedulelib.install([(6, 0)], tmp_path, run=recorder(), path=plist)

    assert written(plist)["RunAtLoad"] is False


def test_the_agent_names_an_absolute_command_and_the_home_it_watches(
        tmp_path, plist):
    """launchd shares almost none of a shell's environment, so nothing may be
    left to it — not the PATH the command is found on, and not the folder the
    watcher would otherwise read from one."""
    schedulelib.install([(6, 0)], tmp_path, run=recorder(), path=plist)

    command = written(plist)["ProgramArguments"]

    assert command == [COMMAND, "--home", str(tmp_path.resolve()), "once"]


def test_the_agents_output_is_kept_in_the_watch_home(tmp_path, plist):
    schedulelib.install([(6, 0)], tmp_path, run=recorder(), path=plist)

    agent = written(plist)
    expected = str(tmp_path / schedulelib.LAUNCHD_LOG)
    assert agent["StandardOutPath"] == expected
    assert agent["StandardErrorPath"] == expected


def test_libreoffice_can_still_be_found_from_a_launchd_run(tmp_path, plist):
    """Prep shells out to LibreOffice for a .doc or an .odt. A Homebrew
    install answers to `which` and to nothing else in an empty environment."""
    schedulelib.install([(6, 0)], tmp_path, run=recorder(), path=plist)

    path = written(plist)["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in path and "/usr/local/bin" in path


# --- loading and unloading ----------------------------------------------------

def test_a_schedule_is_unloaded_before_it_is_loaded(tmp_path, plist):
    """So changing the times replaces the old agent rather than being refused
    for one already existing."""
    run = recorder()

    schedulelib.install([(6, 0)], tmp_path, run=run, path=plist)

    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]
    assert run.calls[0][2].endswith(f"/{LABEL}")
    assert run.calls[1][3] == str(plist)


def test_nothing_loaded_to_unload_is_not_an_error(tmp_path, plist):
    """The ordinary case the first time."""
    run = recorder(fails={"bootout"})

    assert schedulelib.install([(6, 0)], tmp_path, run=run, path=plist) == plist


def test_a_schedule_macos_refuses_says_what_failed(tmp_path, plist):
    run = recorder(fails={"bootstrap"})

    with pytest.raises(ScheduleError, match="Bootstrap failed"):
        schedulelib.install([(6, 0)], tmp_path, run=run, path=plist)

    assert plist.exists()          # left behind, and the message says where


def test_removing_a_schedule_unloads_it_and_takes_the_file_away(tmp_path,
                                                                plist):
    run = recorder()
    schedulelib.install([(6, 0)], tmp_path, run=run, path=plist)
    run.calls.clear()

    assert schedulelib.uninstall(run=run, path=plist) is True
    assert not plist.exists()
    assert run.calls[0][1] == "bootout"


def test_removing_a_schedule_that_was_never_there_is_not_a_failure(plist):
    run = recorder(fails={"bootout"})

    assert schedulelib.uninstall(run=run, path=plist) is False


# --- reading it back ----------------------------------------------------------

def test_the_times_can_be_read_back_off_the_agent(tmp_path, plist):
    schedulelib.install(schedulelib.parse_times("06:00,21:30"), tmp_path,
                        run=recorder(), path=plist)

    assert schedulelib.current(path=plist) == [(6, 0), (21, 30)]


def test_no_agent_reads_back_as_no_schedule(plist):
    assert schedulelib.current(path=plist) is None


def test_an_unreadable_agent_is_not_a_crash(plist, caplog):
    plist.write_bytes(b"not a plist")

    assert schedulelib.current(path=plist) is None
    assert "unreadable" in caplog.text.lower()


def test_the_times_are_described_the_way_they_were_typed():
    assert schedulelib.describe([(6, 0), (16, 30)]) == "06:00, 16:30"


# --- which docproof-watch -----------------------------------------------------

def test_the_copy_running_now_is_the_copy_that_gets_scheduled(tmp_path,
                                                              monkeypatch):
    """A machine with two virtualenvs would otherwise get whichever one is
    earlier in a PATH launchd does not share."""
    pretend = tmp_path / "docproof-watch"
    pretend.write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.argv", [str(pretend), "schedule"])

    assert REAL_EXECUTABLE() == str(pretend.resolve())


def test_the_one_on_the_path_will_do_when_this_is_not_that_command(
        tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr("shutil.which", lambda name: COMMAND)

    assert REAL_EXECUTABLE() == COMMAND


def test_a_docproof_watch_that_is_not_installed_says_how_to_install_it(
        monkeypatch):
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(ScheduleError, match="pip install"):
        REAL_EXECUTABLE()
