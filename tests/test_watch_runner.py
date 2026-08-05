"""The watcher held open inside the app.

Two claims are load-bearing here and everything else is detail. One: the app
never keeps the folder between passes, because the scheduled agent has to be
able to run while a window is open — that is the whole arrangement. Two: one
pass at a time, whichever door it came in by.

Nothing here sleeps on a real clock; `consider()` is public precisely so the
decision can be asked for directly.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import Job, JobStore
from app.lock import FolderLock
from app.settings import Paths
from app.watch import tick as ticklib
from app.watch.drive import AuthExpired, DriveError
from app.watch.runner import WatchRunner
from app.watch.settings import WatchSettings
from app.watch.state import note_tick
from app.watch.tick import TickReport


def configured(home, **over) -> WatchSettings:
    fields = {"folder_id": "1AbCdEfGhIjKlMnOp", "client_id": "c",
              "client_secret": "s", **over}
    ws = WatchSettings(**fields)
    ws.save(home)
    return ws


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A watcher whose tick, sign-in and keychain are all stand-ins."""
    monkeypatch.setattr("app.watch.runner.missing", lambda ws, **kw: None)
    r = WatchRunner(tmp_path)
    r._tick = lambda home, ws, **kw: TickReport(listed=3, new=1,
                                                prepped=["Wolves.docx"])
    r._sign_in = lambda home, cid, secret, **kw: None
    return r


def blocking(gate: threading.Event, entered: threading.Event, calls=None):
    """A tick that stops until it is let go, so a race can be arranged."""
    def tick(home, ws, **kw):
        if calls is not None:
            calls.append(1)
        entered.set()
        gate.wait(5)
        return TickReport(listed=1, new=1, prepped=["Wolves.docx"])
    return tick


# --- the folder ---------------------------------------------------------------

def test_the_watcher_does_not_hold_the_folder_between_passes(runner, tmp_path):
    """The single most important thing in this file. A scheduled pass started
    by macOS must be able to claim the folder while DocProof sits open; if the
    app held the lock for its own lifetime, nothing would ever run when the
    window was."""
    runner.pass_once()

    with FolderLock(tmp_path):            # would raise if the app kept it
        pass


def test_the_folder_is_given_back_even_when_a_pass_fails(runner, tmp_path):
    def explode(home, ws, **kw):
        raise DriveError("Google said no.")
    runner._tick = explode

    out = runner.pass_once()

    assert not out.ok and out.error_kind == "drive"
    with FolderLock(tmp_path):
        pass


def test_a_pass_that_finds_the_folder_claimed_stands_aside(runner, tmp_path):
    """Not a failure. A long book can outlast the gap between two scheduled
    runs, and letting the first finish is the right answer."""
    with FolderLock(tmp_path):
        out = runner.pass_once()

    assert out.skipped and out.error_kind == "in_use"
    assert out.ok                          # standing aside is not going wrong


# --- one at a time ------------------------------------------------------------

def test_one_pass_at_a_time(runner):
    gate, entered, calls = threading.Event(), threading.Event(), []
    runner._tick = blocking(gate, entered, calls)

    assert runner.run_now() is True
    entered.wait(5)
    assert runner.run_now() is False       # the second click does nothing
    gate.set()
    runner.wait_idle()

    assert len(calls) == 1


def test_the_folder_is_free_again_once_a_pass_finishes(runner, tmp_path):
    runner.run_now()
    runner.wait_idle()

    assert not runner.busy
    with FolderLock(tmp_path):
        pass


def test_what_the_last_pass_did_is_kept(runner):
    runner.run_now()
    runner.wait_idle()

    last = runner.state()["last"]
    assert last["prepped"] == ["Wolves.docx"]
    assert last["ok"] and last["started_at"] and last["finished_at"]


def test_stopping_does_not_wait_on_a_pass_that_is_mid_book(runner):
    """Quitting kills the thread, and that is safe: the checkpoint replays what
    was paid for and the Drive marker is written last."""
    gate, entered = threading.Event(), threading.Event()
    runner._tick = blocking(gate, entered)
    runner.run_now()
    entered.wait(5)

    runner.stop()                          # returns rather than blocking

    gate.set()
    runner.wait_idle()


# --- how a failure is described -----------------------------------------------

@pytest.mark.parametrize("error,kind", [
    (AuthExpired("gone"), "auth_expired"),
    (ticklib.NotConfigured("no folder"), "not_configured"),
    (DriveError("busy"), "drive"),
    (RuntimeError("something nobody predicted"), "other"),
])
def test_every_failure_is_given_a_kind_the_panel_can_speak_for(runner, error,
                                                               kind):
    def explode(home, ws, **kw):
        raise error
    runner._tick = explode

    out = runner.pass_once()

    assert out.error_kind == kind and not out.ok
    assert out.error                       # the library's own words, kept


# --- the clock ----------------------------------------------------------------

def test_the_clock_stays_quiet_unless_it_was_asked(runner, tmp_path):
    configured(tmp_path, auto_ticks=False)

    assert runner.consider() is False


def test_the_clock_stays_quiet_while_nothing_is_set_up(tmp_path, monkeypatch):
    """A half-configured watcher should not fail into the log every hour."""
    r = WatchRunner(tmp_path)
    r._tick = lambda *a, **kw: TickReport()
    configured(tmp_path, auto_ticks=True, folder_id="")

    assert r.consider() is False


def test_the_clock_looks_when_it_is_due(runner, tmp_path):
    configured(tmp_path, auto_ticks=True)

    assert runner.consider() is True
    runner.wait_idle()
    assert runner.state()["last"]["prepped"] == ["Wolves.docx"]


def test_the_clock_does_not_repeat_a_pass_the_agent_just_made(runner, tmp_path):
    """Both clocks are allowed to run. Neither should repeat what the other
    did a minute ago — the lock would make it safe, but not quiet."""
    configured(tmp_path, auto_ticks=True, tick_every_minutes=60)
    note_tick(tmp_path)

    assert runner.consider() is False


def test_the_clock_looks_again_once_enough_time_has_passed(runner, tmp_path):
    configured(tmp_path, auto_ticks=True, tick_every_minutes=60)
    stale = datetime.now(timezone.utc) - timedelta(minutes=61)
    (tmp_path / "last_tick").write_text(stale.isoformat(), encoding="utf-8")

    assert runner.consider() is True
    runner.wait_idle()


def test_the_clock_does_not_start_a_second_pass_over_a_running_one(runner,
                                                                   tmp_path):
    configured(tmp_path, auto_ticks=True)
    gate, entered = threading.Event(), threading.Event()
    runner._tick = blocking(gate, entered)
    runner.run_now()
    entered.wait(5)

    assert runner.consider() is False

    gate.set()
    runner.wait_idle()


# --- signing in ---------------------------------------------------------------

def test_a_sign_in_reports_while_it_waits_then_says_it_is_done(runner):
    gate = threading.Event()
    entered = threading.Event()

    def slow(home, cid, secret, **kw):
        entered.set()
        gate.wait(5)

    runner._sign_in = slow

    assert runner.begin_sign_in("id", "secret").state == "waiting"
    entered.wait(5)
    assert runner.sign_in_state().state == "waiting"

    gate.set()
    for _ in range(500):
        if runner.sign_in_state().state != "waiting":
            break
        threading.Event().wait(0.01)

    assert runner.sign_in_state().state == "done"


def test_a_second_sign_in_while_one_waits_does_not_open_a_second_browser(runner):
    gate, entered = threading.Event(), threading.Event()
    opened = []

    def slow(home, cid, secret, **kw):
        opened.append(1)
        entered.set()
        gate.wait(5)

    runner._sign_in = slow
    runner.begin_sign_in("id", "secret")
    entered.wait(5)

    runner.begin_sign_in("id", "secret")

    gate.set()
    assert len(opened) == 1


def test_a_sign_in_that_was_refused_says_why(runner):
    def refuse(home, cid, secret, **kw):
        raise AuthExpired("The sign-in was declined in the browser.")

    runner._sign_in = refuse
    runner.begin_sign_in("id", "secret")
    for _ in range(500):
        if runner.sign_in_state().state != "waiting":
            break
        threading.Event().wait(0.01)

    said = runner.sign_in_state()
    assert said.state == "failed"
    assert "declined" in said.message      # Google's own words, kept


# --- what the panel reads -----------------------------------------------------

def test_reading_the_state_creates_nothing(tmp_path):
    """The panel asks this every five seconds."""
    r = WatchRunner(tmp_path / "never-used")

    assert r.state()["progress"] == []
    assert not (tmp_path / "never-used").exists()


def test_progress_is_read_off_the_job_the_pass_is_writing(tmp_path):
    """Free, and true: a pass updates done/total on the record as it goes, so
    this is the same number it is counting."""
    r = WatchRunner(tmp_path)
    store = JobStore(Paths(tmp_path))
    store.save(Job(id="j-1", filename="Wolves.docx", source_path="/x",
                   model="claude-haiku-4-5", mode="now", kind="prep",
                   state="running", done=2, total=5))

    rows = r.state()["progress"]

    assert rows[0]["filename"] == "Wolves.docx"
    assert rows[0]["plain_state"] == "Reading your manuscript (2 of 5)"


def test_a_finished_job_is_not_progress(tmp_path):
    r = WatchRunner(tmp_path)
    store = JobStore(Paths(tmp_path))
    store.save(Job(id="j-1", filename="Wolves.docx", source_path="/x",
                   model="claude-haiku-4-5", mode="now", kind="prep",
                   state="done"))

    assert r.state()["progress"] == []
    assert len(r.jobs()) == 1


def test_a_watcher_that_has_never_run_has_no_jobs(tmp_path):
    assert WatchRunner(tmp_path / "nothing").jobs() == []
