"""The DocWatch panel, over HTTP.

Same rules as test_app.py: nothing reaches a vendor, nothing sleeps, and
nothing writes to ~/Library/LaunchAgents. The thread that a pass runs on is
real, but every tick it would call is a stand-in.

The claim this file exists to hold: **an app user has no terminal.** The
library's refusals name a command to type, which is right for the only caller
that shows them to somebody holding one. Everything that reaches the panel has
to name a card instead.
"""
from __future__ import annotations

import logging
import plistlib
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobStore
from app.main import create_app
from app.settings import Paths
from app.watch import tick as ticklib
from app.watch.drive import AuthExpired, DriveError
from app.watch.settings import WatchSettings
from app.watch.state import FileRecord, WatchState
from app.watch.tick import TickReport

FOLDER = "1AbCdEfGhIjKlMnOp"
FOLDER_URL = f"https://drive.google.com/drive/u/0/folders/{FOLDER}?usp=sharing"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app whose watcher never reaches Google, launchctl or the Keychain."""
    monkeypatch.delenv("DOCPROOF_WATCH_HOME", raising=False)
    monkeypatch.setattr("app.main.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.main.key_status", lambda: {})
    monkeypatch.setattr("app.watch.status.get_api_key",
                        lambda name: "refresh-1")

    home = tmp_path / "watch"
    app = create_app(tmp_path / "app", start_runner=False, watch_home=home)
    watch = app.state.watch
    watch.agent_path = tmp_path / "agent.plist"
    watch.launchctl = lambda command, **kw: subprocess.CompletedProcess(
        command, 0, stdout="", stderr="")
    watch._tick = lambda h, ws, **kw: TickReport(listed=3, new=1,
                                                 prepped=["Wolves.docx"],
                                                 uploaded=["tagged_Wolves.docx"])
    watch._sign_in = lambda h, cid, secret, **kw: None
    monkeypatch.setattr("app.watch.schedule.executable",
                        lambda: "/usr/local/bin/docproof-watch")

    with TestClient(app) as c:
        c.app_state = app.state
        c.home = home
        yield c


def configured(client, **over) -> WatchSettings:
    fields = {"folder_id": FOLDER, "model": "claude-haiku-4-5",
              "client_id": "c", "client_secret": "s", **over}
    ws = WatchSettings(**fields)
    ws.save(client.home)
    return ws


def watch_of(client) -> dict:
    return client.get("/api/watch").json()


# --- before anything is set up ------------------------------------------------

def test_the_panel_answers_before_anything_is_set_up(client):
    body = watch_of(client)

    assert body["watch"]["missing"] == "folder"
    assert body["watch"]["files"] == []
    assert body["run"]["busy"] is False
    assert body["sign_in"] is None


def test_asking_what_the_watcher_has_done_creates_nothing(client, tmp_path):
    """Every five seconds, all session. A DocProof nobody has pointed at a
    Drive should not grow a watch home because a tab was opened."""
    watch_of(client)

    assert not (tmp_path / "watch").exists()


def test_a_refusal_never_tells_an_app_user_to_type_a_command(client):
    """The whole reason the route writes its own prose."""
    detail = client.post("/api/watch/run").json()["detail"]

    assert "docproof-watch" not in detail
    assert "card on this screen" in detail


def test_a_pass_with_no_sign_in_says_to_sign_in(client):
    configured(client, client_id="", client_secret="")

    detail = client.post("/api/watch/run").json()["detail"]

    assert "Sign in to Google" in detail
    assert "docproof-watch" not in detail


def test_a_preview_with_nothing_set_up_is_refused_the_same_way(client):
    assert client.post("/api/watch/preview").status_code == 400


# --- saying what to watch -----------------------------------------------------

def test_the_folder_can_be_pasted_as_a_browser_address(client):
    body = client.put("/api/watch", json={"folder": FOLDER_URL}).json()

    assert body["watch"]["folder_id"] == FOLDER
    assert WatchSettings.load(client.home).folder_id == FOLDER


def test_a_blank_folder_box_means_unchanged_not_an_error(client):
    """A fresh panel has an empty folder box, and Save always sends the
    field. Refusing '' used to throw away the model and output choices
    saved alongside it."""
    answer = client.put("/api/watch", json={"folder": "",
                                            "prep_output": "both"})

    assert answer.status_code == 200
    assert WatchSettings.load(client.home).prep_output == "both"


def test_something_that_is_not_a_folder_is_refused(client):
    answer = client.put("/api/watch", json={"folder": "my documents"})

    assert answer.status_code == 400
    assert "paste the whole address" in answer.json()["detail"]


def test_a_model_docproof_does_not_know_is_refused(client):
    answer = client.put("/api/watch", json={"model": "gpt-9"})

    assert answer.status_code == 400
    assert WatchSettings.load(client.home).model == WatchSettings().model


def test_saving_one_thing_keeps_the_rest(client):
    configured(client, model="claude-opus-5")

    client.put("/api/watch", json={"prep_output": "both"})

    ws = WatchSettings.load(client.home)
    assert ws.model == "claude-opus-5" and ws.prep_output == "both"
    assert ws.folder_id == FOLDER and ws.client_id == "c"


def test_a_pass_that_would_do_too_much_at_once_is_refused(client):
    """A bound in the schema, because a slip in the UI should not be able to
    spend a morning's worth of manuscripts in one pass."""
    assert client.put("/api/watch",
                      json={"max_files_per_tick": 500}).status_code == 422


def test_both_clocks_can_be_set_from_the_panel(client):
    client.put("/api/watch", json={"auto_ticks": True,
                                   "tick_every_minutes": 30})

    ws = WatchSettings.load(client.home)
    assert ws.auto_ticks is True and ws.tick_every_minutes == 30


# --- signing in ---------------------------------------------------------------

def test_signing_in_reports_while_it_waits_then_says_it_is_done(client):
    configured(client)
    gate, entered = threading.Event(), threading.Event()

    def slow(home, cid, secret, **kw):
        entered.set()
        gate.wait(5)

    client.app_state.watch._sign_in = slow

    body = client.post("/api/watch/auth", json={}).json()
    assert body["sign_in"]["state"] == "waiting"
    entered.wait(5)

    gate.set()
    for _ in range(500):
        if watch_of(client)["sign_in"]["state"] != "waiting":
            break
        threading.Event().wait(0.01)

    assert watch_of(client)["sign_in"]["state"] == "done"


def test_a_second_sign_in_while_one_waits_opens_no_second_browser(client):
    configured(client)
    gate, entered, opened = threading.Event(), threading.Event(), []

    def slow(home, cid, secret, **kw):
        opened.append(1)
        entered.set()
        gate.wait(5)

    client.app_state.watch._sign_in = slow
    client.post("/api/watch/auth", json={})
    entered.wait(5)

    client.post("/api/watch/auth", json={})

    gate.set()
    assert len(opened) == 1


def test_a_sign_in_that_was_declined_says_so_in_googles_words(client):
    configured(client)

    def refuse(home, cid, secret, **kw):
        raise AuthExpired("The sign-in was declined in the browser.")

    client.app_state.watch._sign_in = refuse
    client.post("/api/watch/auth", json={})
    for _ in range(500):
        if watch_of(client)["sign_in"]["state"] != "waiting":
            break
        threading.Event().wait(0.01)

    said = watch_of(client)["sign_in"]
    assert said["state"] == "failed" and "declined" in said["message"]


def test_signing_in_needs_an_oauth_client(client):
    answer = client.post("/api/watch/auth", json={})

    assert answer.status_code == 400
    assert "Google Cloud console" in answer.json()["detail"]


def test_the_client_can_be_given_with_the_request(client):
    gate = threading.Event()
    seen = {}

    def record(home, cid, secret, **kw):
        seen["id"], seen["secret"] = cid, secret
        gate.set()

    client.app_state.watch._sign_in = record
    client.post("/api/watch/auth", json={"client_id": "new-id",
                                         "client_secret": "new-secret"})
    gate.wait(5)

    assert seen == {"id": "new-id", "secret": "new-secret"}


def test_signing_out_forgets_the_token_but_keeps_the_client(client,
                                                            monkeypatch):
    """The client id and secret are not the secret — an installed application
    cannot keep one — and signing in again needs them."""
    configured(client)
    forgotten = []
    monkeypatch.setattr("app.main.delete_api_key", forgotten.append)

    client.delete("/api/watch/auth")

    assert forgotten == ["google"]
    assert WatchSettings.load(client.home).client_id == "c"


# --- a pass -------------------------------------------------------------------

def test_a_pass_runs_in_the_background_and_the_panel_can_watch_it(client):
    configured(client)
    gate, entered = threading.Event(), threading.Event()

    def slow(home, ws, **kw):
        entered.set()
        gate.wait(5)
        return TickReport(listed=2, new=1, prepped=["Wolves.docx"])

    client.app_state.watch._tick = slow

    body = client.post("/api/watch/run").json()
    assert body["started"] is True
    entered.wait(5)
    assert watch_of(client)["run"]["busy"] is True

    gate.set()
    client.app_state.watch.wait_idle()

    assert watch_of(client)["run"]["last"]["prepped"] == ["Wolves.docx"]


def test_a_second_look_while_one_is_running_is_not_a_second_pass(client):
    configured(client)
    gate, entered = threading.Event(), threading.Event()

    def slow(home, ws, **kw):
        entered.set()
        gate.wait(5)
        return TickReport()

    client.app_state.watch._tick = slow
    client.post("/api/watch/run")
    entered.wait(5)

    answer = client.post("/api/watch/run")

    assert answer.status_code == 200          # not an error: it is a double click
    assert answer.json()["started"] is False
    gate.set()
    client.app_state.watch.wait_idle()


def test_the_panel_shows_progress_while_a_manuscript_is_prepared(client):
    """The pass already writes this as it goes; the panel only reads it."""
    configured(client)
    JobStore(Paths(client.home)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep",
            state="running", done=2, total=5))

    row = watch_of(client)["run"]["progress"][0]

    assert row["filename"] == "Wolves.docx"
    assert row["plain_state"] == "Reading your manuscript (2 of 5)"


def test_a_revoked_sign_in_is_reported_in_the_apps_own_words(client):
    configured(client)

    def expired(home, ws, **kw):
        raise AuthExpired("Run `docproof-watch auth` to sign in again.")

    client.app_state.watch._tick = expired
    client.post("/api/watch/run")
    client.app_state.watch.wait_idle()

    last = watch_of(client)["run"]["last"]
    assert last["error_kind"] == "auth_expired" and last["ok"] is False


def test_a_pass_that_stood_aside_is_not_a_failure(client):
    configured(client)
    from app.lock import FolderLock

    client.home.mkdir(parents=True, exist_ok=True)
    with FolderLock(client.home):
        client.app_state.watch.pass_once()

    last = watch_of(client)["run"]["last"]
    assert last["skipped"] is True and last["ok"] is True


def test_what_has_been_prepared_is_listed(client):
    configured(client)
    JobStore(Paths(client.home)).save(
        Job(id="j-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="done",
            cost=0.41))
    WatchState(client.home / "state.json").record(
        FileRecord(file_id="f-1", name="Wolves.docx", job_id="j-1",
                   marked="formatted", uploaded={"tagged_Wolves.docx": "u-1"}))

    row = watch_of(client)["watch"]["files"][0]

    assert row["name"] == "Wolves.docx" and row["cost"] == 0.41
    assert row["plain_state"] == "Prepared"


# --- a preview ----------------------------------------------------------------

def test_a_preview_says_what_a_pass_would_do_and_touches_nothing(client,
                                                                 tmp_path):
    configured(client)
    client.app_state.watch._tick = lambda h, ws, **kw: TickReport(
        listed=2, new=1, dry_run=True,
        plan=[("Wolves.docx", "new"), ("cover.png", "skip")])

    body = client.post("/api/watch/preview").json()

    assert body["new"] == 1
    assert body["plan"][0] == {"name": "Wolves.docx", "stage": "new",
                               "label": "to prepare"}
    assert body["plan"][1]["label"] == "not a manuscript"
    assert not (client.home / "jobs").exists()


def test_a_preview_google_refuses_says_what_google_said(client):
    configured(client)

    def refuse(home, ws, **kw):
        raise DriveError("Google Drive is busy and would not read the folder.")

    client.app_state.watch._tick = refuse
    answer = client.post("/api/watch/preview")

    assert answer.status_code == 400
    assert "busy" in answer.json()["detail"]


# --- automatic passes ---------------------------------------------------------

def test_turning_the_schedule_on_writes_an_agent_and_says_when(client,
                                                               tmp_path):
    configured(client)

    body = client.put("/api/watch/schedule",
                      json={"times": "06:00,21:00"}).json()

    assert body["watch"]["times"] == ["06:00", "21:00"]
    agent = plistlib.loads((tmp_path / "agent.plist").read_bytes())
    assert agent["StartCalendarInterval"] == [{"Hour": 6, "Minute": 0},
                                              {"Hour": 21, "Minute": 0}]
    assert agent["RunAtLoad"] is False


def test_a_time_that_does_not_exist_is_refused_without_writing_an_agent(
        client, tmp_path):
    configured(client)

    answer = client.put("/api/watch/schedule", json={"times": "25:00"})

    assert answer.status_code == 400
    assert "no such time" in answer.json()["detail"]
    assert not (tmp_path / "agent.plist").exists()


def test_turning_the_schedule_off_removes_it(client, tmp_path):
    configured(client)
    client.put("/api/watch/schedule", json={"times": "06:00"})

    body = client.delete("/api/watch/schedule").json()

    assert body["watch"]["times"] == []
    assert not (tmp_path / "agent.plist").exists()


def test_scheduling_anywhere_but_a_mac_says_so(client, monkeypatch):
    configured(client)
    monkeypatch.setattr("app.main.sys.platform", "linux")

    answer = client.put("/api/watch/schedule", json={"times": "06:00"})

    assert answer.status_code == 501
    assert "needs a Mac" in answer.json()["detail"]


# --- what the app must not do to itself ---------------------------------------

def test_the_app_never_clears_the_logging_the_watcher_cli_clears(client):
    """`cli._logging` calls handlers.clear(). A long-lived server that lost its
    handlers would stop logging for the rest of the day, so nothing here may
    reach that function."""
    log = logging.getLogger("docproof")
    before = list(log.handlers)

    configured(client)
    watch_of(client)
    client.post("/api/watch/run")
    client.app_state.watch.wait_idle()
    client.post("/api/watch/preview")

    assert log.handlers == before


def test_an_app_built_without_a_runner_starts_no_watcher(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCPROOF_WATCH_HOME", raising=False)
    app = create_app(tmp_path, start_runner=False)

    assert app.state.watch.started is False


def test_the_watcher_home_sits_beside_the_app_home(tmp_path, monkeypatch):
    """So an app started with --home somewhere gets a watcher beside it rather
    than reaching for the real one."""
    monkeypatch.delenv("DOCPROOF_WATCH_HOME", raising=False)
    app = create_app(tmp_path, start_runner=False)

    assert app.state.watch.home == tmp_path / "watch"


def test_the_environment_still_says_where_the_watcher_lives(tmp_path,
                                                            monkeypatch):
    """The terminal honours DOCPROOF_WATCH_HOME, and the two doors have to
    agree about which folder they are both looking at."""
    monkeypatch.setenv("DOCPROOF_WATCH_HOME", str(tmp_path / "elsewhere"))
    app = create_app(tmp_path, start_runner=False)

    assert app.state.watch.home == tmp_path / "elsewhere"


# --- one bill -----------------------------------------------------------------

def test_the_dashboard_adds_the_watchers_spending_to_the_apps(client):
    """Two job stores, one card."""
    JobStore(Paths(client.home)).save(
        Job(id="w-1", filename="Wolves.docx", source_path="/x",
            model="claude-haiku-4-5", mode="now", kind="prep", state="done",
            source="watch", api_calls=3, input_tokens=1000,
            output_tokens=100, cost=0.41))

    usage = client.get("/api/usage").json()

    assert usage["totals"]["jobs"] == 1
    assert [row["source"] for row in usage["by_source"]] == ["watch"]
    assert usage["recent"][0]["source"] == "watch"


def test_a_dashboard_with_no_watcher_at_all_is_still_zeros(client):
    usage = client.get("/api/usage").json()

    assert usage["totals"]["jobs"] == 0
    assert usage["by_source"] == []
