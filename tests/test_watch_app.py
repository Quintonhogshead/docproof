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
    monkeypatch.setattr("app.settings.get_api_key", lambda p: "test-key")
    monkeypatch.setattr("app.settings.key_status", lambda: {})
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


def test_the_test_email_button_sends_via_the_watchers_account(client,
                                                              monkeypatch):
    """The panel's "Send test email" posts here; the route sends through the
    watcher's own Google account and answers with where it went."""
    monkeypatch.setattr("app.watch.notify.send_test",
                        lambda home, **kw: "quinton@atmospherepress.com")

    body = client.post("/api/watch/test-email").json()

    assert body == {"sent": True, "to": "quinton@atmospherepress.com"}


def test_a_test_email_with_no_address_names_the_gap(client):
    """No notify address set: a clear 400 that names the fix, and no network."""
    answer = client.post("/api/watch/test-email")
    assert answer.status_code == 400
    assert "notify address" in answer.json()["detail"]


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


def test_require_source_label_can_be_set_from_the_panel(client):
    body = client.put("/api/watch", json={"require_source_label": True}).json()

    assert WatchSettings.load(client.home).require_source_label is True
    # And it comes back in the payload the panel reads, so the box reflects it.
    assert body["watch"]["require_source_label"] is True


def test_both_clocks_can_be_set_from_the_panel(client):
    client.put("/api/watch", json={"auto_ticks": True,
                                   "tick_every_minutes": 30})

    ws = WatchSettings.load(client.home)
    assert ws.auto_ticks is True and ws.tick_every_minutes == 30


def test_the_in_app_clock_can_be_given_set_times(client):
    """The server's only schedule: fixed times of day, no launchd involved."""
    body = client.put("/api/watch", json={
        "auto_ticks": True,
        "tick_at_times": ["17:00", "9:00", "09:00"],   # unsorted, unpadded, dup
        "tick_timezone": "America/New_York",
    }).json()

    ws = WatchSettings.load(client.home)
    # Normalised on the way in: padded, deduped and sorted.
    assert ws.tick_at_times == ["09:00", "17:00"]
    assert ws.tick_timezone == "America/New_York"
    assert body["watch"]["tick_at_times"] == ["09:00", "17:00"]
    # And the panel is handed a next-look it can show without doing the maths.
    assert body["watch"]["next_tick_at"]


def test_an_empty_times_list_hands_the_interval_clock_its_job_back(client):
    configured(client, tick_at_times=["09:00"])

    client.put("/api/watch", json={"tick_at_times": []})

    assert WatchSettings.load(client.home).tick_at_times == []


def test_a_bad_time_is_refused(client):
    answer = client.put("/api/watch", json={"tick_at_times": ["nine o'clock"]})

    assert answer.status_code == 400
    assert WatchSettings.load(client.home).tick_at_times == []


def test_a_bad_time_zone_is_refused(client):
    answer = client.put("/api/watch", json={"tick_timezone": "Mars/Olympus"})

    assert answer.status_code == 400
    assert WatchSettings.load(client.home).tick_timezone == ""


def test_the_archive_can_be_set_up_from_the_panel(client):
    body = client.put("/api/watch", json={
        "archive_enabled": True,
        "archive_folder": "https://drive.google.com/drive/folders/ARCH1234567",
        "archive_include_source": False,
    }).json()

    ws = WatchSettings.load(client.home)
    assert ws.archive_enabled is True
    assert ws.archive_folder_id == "ARCH1234567"   # parsed out of the address
    assert ws.archive_include_source is False
    # And the panel reads the settings back for the page to show.
    assert body["watch"]["archive_enabled"] is True
    assert body["watch"]["archive_folder_id"] == "ARCH1234567"


def test_a_bad_archive_folder_is_refused(client):
    answer = client.put("/api/watch", json={"archive_folder": "my documents"})
    assert answer.status_code == 400
    assert WatchSettings.load(client.home).archive_folder_id == ""


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
    monkeypatch.setattr("app.settings.delete_api_key", forgotten.append)

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
    monkeypatch.setattr("sys.platform", "linux")

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


def test_the_dashboard_counts_a_cleared_watch_job_from_its_own_ledger(client):
    """The watcher's spend outlives its jobs: a DocWatch job that has been
    cleared lives on only in the watch store's own ledger. The bill still counts
    it — before, only the app ledger was read, so a cleared watch job vanished."""
    from app.spending import LedgerEntry, SpendingLedger
    store = JobStore(Paths(client.home))
    SpendingLedger(store.paths.spending_db).record(LedgerEntry(
        id="w-cleared", filename="Bears.docx", kind="promo",
        model="claude-haiku-4-5", mode="now", source="watch", owner_id="",
        created_at="2026-08-10T00:00:00+00:00", words=0, input_tokens=0,
        output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
        api_calls=1, cost=0.41))

    usage = client.get("/api/usage").json()

    assert usage["totals"]["jobs"] == 1
    assert usage["totals"]["cost"] == pytest.approx(0.41)
    assert [row["source"] for row in usage["by_source"]] == ["watch"]


def test_a_dashboard_with_no_watcher_at_all_is_still_zeros(client):
    usage = client.get("/api/usage").json()

    assert usage["totals"]["jobs"] == 0
    assert usage["by_source"] == []


# --- the proofing panel -------------------------------------------------------
#
# The Automations screen's Proofread workflow. What it needs from the API is a
# way to turn the stage on, choose who reads the book, and correct the three
# HubSpot values without a terminal on the Fly volume — plus enough of the
# status payload to show which books are out with a practitioner and how the
# last few proofreads ended.

def test_the_panel_can_turn_proofing_on_and_choose_the_runner(client):
    configured(client)

    body = client.put("/api/watch", json={"proofing_enabled": True,
                                          "proof_runner": "external"}).json()

    assert body["watch"]["proofing_enabled"] is True
    assert body["watch"]["proof_runner"] == "external"
    ws = WatchSettings.load(client.home)
    assert ws.proofing_enabled is True and ws.proof_runner == "external"


def test_a_runner_nobody_has_heard_of_is_refused(client):
    answer = client.put("/api/watch", json={"proof_runner": "magic"})

    assert answer.status_code == 400
    assert "practitioner" in answer.json()["detail"]
    assert WatchSettings.load(client.home).proof_runner == "external"


def test_the_panel_can_correct_the_three_hubspot_values(client):
    """The vocabulary is newer than the panel, so an admin has to be able to fix
    a value they typed into HubSpot without ssh-ing onto the volume."""
    configured(client)

    body = client.put("/api/watch", json={
        "hubspot_proof_ready_value": "Ready for Proofing ",
        "hubspot_proof_done_value": " Formatting/Proofing Complete",
        "hubspot_proof_needs_human_value": "Needs Human PR",
    }).json()

    ws = WatchSettings.load(client.home)
    assert ws.hubspot_proof_ready_value == "Ready for Proofing"   # trimmed
    assert ws.hubspot_proof_done_value == "Formatting/Proofing Complete"
    assert ws.hubspot_proof_needs_human_value == "Needs Human PR"
    assert body["watch"]["hubspot_proof_done_value"] == \
        "Formatting/Proofing Complete"


def test_a_blank_needs_human_value_is_a_real_choice(client):
    """Blank means "write nothing for that verdict" — `_finish_hubspot_proof`
    refuses an empty value rather than blanking the status property — so an
    admin clearing the box has to be able to mean it, unlike the folder box
    where blank means "unchanged"."""
    configured(client, hubspot_proof_needs_human_value="Needs Human PR")

    client.put("/api/watch", json={"hubspot_proof_needs_human_value": ""})

    assert WatchSettings.load(client.home).hubspot_proof_needs_human_value == ""


def test_saving_the_proofing_values_keeps_the_formatting_ones(client):
    """The panel may set the proofing vocabulary; it must not be a way to reach
    the formatting pair, or the status property itself."""
    configured(client, hubspot_status_property="docproof",
               hubspot_format_ready_value="Ready for Formatting",
               hubspot_format_done_value="Formatting Complete")

    client.put("/api/watch", json={
        "hubspot_proof_ready_value": "Ready for Proofing",
        "hubspot_status_property": "hs_pipeline_stage",     # not on the model
        "hubspot_format_done_value": "Closed Won",          # not on the model
    })

    ws = WatchSettings.load(client.home)
    assert ws.hubspot_proof_ready_value == "Ready for Proofing"
    assert ws.hubspot_status_property == "docproof"
    assert ws.hubspot_format_done_value == "Formatting Complete"


def test_the_status_says_enough_to_draw_the_proofing_workflow(client):
    configured(client, hubspot_enabled=True, proofing_enabled=True,
               proof_runner="external")

    w = watch_of(client)["watch"]

    assert w["hubspot_enabled"] is True and w["hubspot_write_back"] is True
    assert w["proofing_enabled"] is True and w["proof_runner"] == "external"
    assert w["hubspot_proof_ready_value"] == "Ready for Proofing"
    assert w["hubspot_proof_done_value"] == "Proofing Complete"
    assert w["hubspot_proof_needs_human_value"] == "Needs Human PR"


def test_the_status_lists_what_is_out_with_a_practitioner_and_what_came_back(
        client):
    """The two read-only lists in the drawer, both read off state.json: a book
    marked `awaiting` with the folder it is in and when it went out, and the
    verdicts that have landed with the reason behind each."""
    state = WatchState(client.home / "state.json")
    state.record(FileRecord(file_id="f-1", name="Johnson - Book 1.docx",
                            subfolder_name="Quinton Johnson",
                            proof_marked="awaiting"))
    state.record(FileRecord(file_id="f-2", name="Okafor - Book 1.docx",
                            subfolder_name="Ada Okafor",
                            proof_marked="human", proof_outcome="needs_human",
                            proof_outcome_reason="most sentences must be "
                                                 "rewritten"))
    state.record(FileRecord(file_id="f-3", name="Smith - Book 1.docx",
                            proof_marked="done", proof_outcome="done",
                            proof_outcome_reason="no open items",
                            proof_uploaded={"Smith - Book 2.docx": "up-1"}))

    rows = {r["name"]: r for r in watch_of(client)["watch"]["files"]}

    waiting = rows["Johnson - Book 1.docx"]
    assert waiting["proof_marked"] == "awaiting"
    assert waiting["folder"] == "Quinton Johnson"
    assert waiting["updated_at"]                  # "waiting since"
    assert not waiting["proof_outcome"]           # no verdict yet

    assert rows["Okafor - Book 1.docx"]["proof_outcome"] == "needs_human"
    assert "rewritten" in rows["Okafor - Book 1.docx"]["proof_reason"]
    assert rows["Smith - Book 1.docx"]["proof_outcome"] == "done"
    assert rows["Smith - Book 1.docx"]["proof_uploaded"] == \
        ["Smith - Book 2.docx"]
    # A flat-folder book has no author folder; the panel falls back to naming
    # the watched folder rather than showing an empty cell.
    assert rows["Smith - Book 1.docx"]["folder"] == ""


def test_a_config_written_before_proofing_existed_still_answers(client):
    """The panel has to draw against the prod config as it stands on the Fly
    volume today, which carries none of these keys. `WatchSettings.load` drops
    what it does not know and fills the rest from the defaults, so the payload
    is complete and the stage is off."""
    client.home.mkdir(parents=True, exist_ok=True)
    (client.home / "watch.json").write_text(
        '{"folder_id": "%s", "model": "claude-haiku-4-5", '
        '"hubspot_enabled": true, "unknown_old_key": 1}' % FOLDER,
        encoding="utf-8")

    w = watch_of(client)["watch"]

    assert w["proofing_enabled"] is False         # nothing new happens
    assert w["proof_runner"] == "external"
    assert w["hubspot_proof_ready_value"] == "Ready for Proofing"
    assert w["hubspot_proof_needs_human_value"] == "Needs Human PR"


def test_the_proofing_drawer_and_its_script_name_the_same_elements():
    """The panel is markup in one file and behaviour in another, so a rename on
    either side is a control that silently stops working. This is the seam held
    to: every id the proofing code reaches for exists in the page, and the
    drawer the registry opens is one the page actually has."""
    from app.settings import resource_root

    static = resource_root() / "app" / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")

    for element in ("wf-config-proof", "wf-proof-setup", "wf-proof-save",
                    "wf-proof-note", "proof-enabled", "proof-runner",
                    "proof-runner-hint", "proof-ready", "proof-done",
                    "proof-needs-human", "proof-awaiting-block",
                    "proof-awaiting", "proof-awaiting-empty",
                    "proof-verdicts", "proof-verdicts-empty"):
        assert f'id="{element}"' in page, element
        assert f"'{element}'" in script, element
    # The runner's two values are the ones the API accepts, spelled in the page
    # rather than only in prose.
    assert 'value="external"' in page and 'value="app"' in page


# --- the preview endpoint -----------------------------------------------------
#
# What the panel's "Show me what a pass would do" reads. The headline sentence
# is built from these counts and the table from these rows, so both have to come
# back from one call over one set of rows.

def _preview(client, plan, **counts):
    """Drive the endpoint with a canned dry-run report."""
    client.app_state.watch.preview = lambda: TickReport(
        dry_run=True, listed=len(plan), plan=plan, **counts)
    return client.post("/api/watch/preview").json()


def test_the_preview_counts_every_automation_the_headline_names(client):
    configured(client)

    body = _preview(client, [("a.docx", "new"), ("b.docx", "new"),
                             ("c.docx", "proof"), ("a.docx", "promo"),
                             ("b.docx", "plan"), ("d.docx", "skip")], new=2)

    assert body["new"] == 2
    assert body["proof"] == 1
    assert body["promo"] == 1
    assert body["plan_docs"] == 1
    assert body["listed"] == 6


def test_the_preview_counts_a_row_whose_gate_it_could_not_apply(client):
    """The gate mark is a fact about how sure the preview is, not about what
    kind of row it is — so a hedged row still counts toward its automation."""
    configured(client)

    body = _preview(client, [("a.docx", "new?"), ("c.docx", "proof?"),
                             ("a.docx", "promo?"), ("b.docx", "plan?")], new=1)

    assert body["new"] == 1 and body["proof"] == 1
    assert body["promo"] == 1 and body["plan_docs"] == 1


def test_the_preview_labels_say_when_hubspot_has_not_been_asked(client):
    """The words live in the library, not the page, so the terminal and the
    panel describe the same row the same way."""
    configured(client)

    body = _preview(client, [("a.docx", "new?"), ("b.docx", "promo?"),
                             ("c.docx", "plan?"), ("d.docx", "proof?"),
                             ("e.docx", "new")], new=2)

    labels = {row["name"]: row["label"] for row in body["plan"]}
    assert labels["a.docx"] == "to prepare — if HubSpot says so"
    assert labels["b.docx"] == "to write promo copy for — if HubSpot says so"
    assert labels["c.docx"] == \
        "to write a marketing plan for — if HubSpot says so"
    assert labels["d.docx"] == "to proofread — if HubSpot says so"
    assert labels["e.docx"] == "to prepare"          # nothing to hedge


def test_an_empty_preview_is_zeros_rather_than_missing_keys(client):
    """The page reads all four counts unconditionally; a quiet folder must not
    make the headline read "undefined"."""
    configured(client)

    body = _preview(client, [])

    assert body["new"] == 0 and body["proof"] == 0
    assert body["promo"] == 0 and body["plan_docs"] == 0
    assert body["plan"] == []
