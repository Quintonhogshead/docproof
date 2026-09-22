"""The Warden's four server routes.

One read of the whole picture and three writes that mirror buttons already on
the admin panel — behind a bearer token instead of a browser session, the same
shape as `/api/watch/agent` and `/api/watch/awaiting`, and tested the same way
(see `tests/test_watch_agent_api.py`, which this file's fixtures borrow)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobStore
from app.routes.watch import AGENT_TOKEN_ENV, MIN_AGENT_TOKEN, WARDEN_TOKEN_ENV
from app.settings import ENV_VARS, Paths
from app.watch.settings import GOOGLE_KEY, WatchSettings
from app.watch.state import STATE_FILE, FileRecord, WatchState
from tests.fakes import drive_entry, fake_drive
from tests.test_watch_agent_api import _boss, clean_env, make_app, seed

TOKEN = "a-warden-token-long-enough-to-be-a-secret"


@pytest.fixture(autouse=True)
def clean_warden_env(monkeypatch):
    monkeypatch.delenv(WARDEN_TOKEN_ENV, raising=False)


def finished_record(file_id="source", name="Aragon - Book 1.docx", **kw):
    return FileRecord(file_id=file_id, name=name, marked="formatted", **kw)


# --- who may read it ------------------------------------------------------

def test_a_server_with_no_warden_token_answers_nobody(tmp_path):
    app = make_app(tmp_path)
    answer = TestClient(app).get(
        "/api/watch/warden", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 403
    assert WARDEN_TOKEN_ENV in answer.json()["detail"]


def test_a_short_server_token_is_refused_as_not_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, "test")
    app = make_app(tmp_path)
    answer = TestClient(app).get(
        "/api/watch/warden", headers={"Authorization": "Bearer test"})
    assert answer.status_code == 403
    assert str(MIN_AGENT_TOKEN) in answer.json()["detail"]


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token-but-also-long-enough-yes"},
    {"Authorization": TOKEN},                       # no scheme
    {"Authorization": f"Basic {TOKEN}"},            # wrong scheme
])
def test_anything_but_the_right_bearer_is_refused(tmp_path, monkeypatch, headers):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    answer = TestClient(app).get("/api/watch/warden", headers=headers)
    assert answer.status_code == 401
    assert "watch" not in answer.json()


def test_the_proofing_agents_token_does_not_open_the_wardens_routes(tmp_path, monkeypatch):
    """The two bearer tokens are meant to rotate apart from each other."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(WARDEN_TOKEN_ENV, "a-different-warden-token-thats-long-enough")
    app = make_app(tmp_path)
    answer = TestClient(app).get(
        "/api/watch/warden", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 401


def test_a_browser_session_is_not_the_warden_either(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    assert _boss(app).get("/api/watch/warden").status_code == 401


def test_the_desktop_build_still_wants_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path, web=False)
    client = TestClient(app)
    assert client.get("/api/watch/warden").status_code == 401
    assert client.get(
        "/api/watch/warden",
        headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


# --- payload shape ---------------------------------------------------------

def test_the_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [FileRecord(
        file_id="drive-1", name="Aragon - Book 1.docx", marked="formatted",
        author_first="José", author_last="Aragón", hubspot_id="hs-1",
        job_id="job-1", completion_emailed=True)])

    answer = TestClient(app).get(
        "/api/watch/warden", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200
    body = answer.json()

    assert set(body) == {
        "server_version", "server_time", "watch", "run", "sign_in", "agent",
        "last_pass", "last_tick", "awaiting", "files", "native"}
    assert body["server_version"]
    assert body["server_time"]
    assert isinstance(body["watch"], dict) and body["watch"]["files"]
    assert set(body["run"]) == {"busy", "started_at", "last", "progress"}
    assert body["sign_in"] is None
    assert body["agent"] is None
    assert body["last_pass"] is None
    assert body["last_tick"] is None
    assert body["awaiting"] == []
    assert set(body["native"]) == {"worker", "intake"}
    assert body["native"]["worker"] is None
    assert body["native"]["intake"] is None

    row = body["files"][0]
    assert set(row) == {
        "file_id", "name", "marked", "proof_marked", "corrections_marked",
        "attempts", "updated_at", "author_first", "author_last",
        "subfolder_name", "job_id", "completion_emailed", "hubspot_id"}
    assert row["file_id"] == "drive-1"
    assert row["marked"] == "formatted"
    assert row["author_first"] == "José"
    assert row["author_last"] == "Aragón"
    assert row["hubspot_id"] == "hs-1"
    assert row["job_id"] == "job-1"
    assert row["completion_emailed"] is True


def test_the_answer_carries_no_settings_secrets(tmp_path, monkeypatch):
    """The whole point of a compact, separate readout: no client secret, no
    refresh token, nothing a wider hole than the Warden needs."""
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    ws = WatchSettings.load(home)
    ws.folder_id = "shared-folder"
    ws.client_id = "client-id-marker"
    ws.client_secret = "client-secret-marker"
    ws.save(home)
    monkeypatch.setenv(ENV_VARS[GOOGLE_KEY], "refresh-token-marker")

    raw = TestClient(app).get(
        "/api/watch/warden",
        headers={"Authorization": f"Bearer {TOKEN}"}).text
    assert "client-secret-marker" not in raw
    assert "refresh-token-marker" not in raw


def test_the_answer_lists_the_awaiting_books_too(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [FileRecord(
        file_id="drive-2", name="Test - Book 1.docx", proof_marked="awaiting",
        author_last="Test", subfolder_id="folder-A")])
    books = TestClient(app).get(
        "/api/watch/warden",
        headers={"Authorization": f"Bearer {TOKEN}"}).json()["awaiting"]
    assert len(books) == 1 and books[0]["file_id"] == "drive-2"


# --- flags/reset -------------------------------------------------------------

def test_warden_can_reset_a_finished_flag(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [finished_record()])
    monkeypatch.setattr("app.routes.watch._drive_token_or_none", lambda home: "token")
    opener = fake_drive({"source": drive_entry(
        "Aragon - Book 1.docx", props={"docproof.state": "formatted"})})
    monkeypatch.setattr("app.watch.drive._open_url", opener)

    answer = TestClient(app).post(
        "/api/watch/warden/flags/reset",
        json={"file_id": "source", "stage": "format"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"cleared": "source", "name": "Aragon - Book 1.docx",
                             "stage": "format"}
    back = WatchState.load(home / STATE_FILE).get("source")
    assert back.marked == ""
    assert back.flag_reset_history[0]["by"] == "the Warden"


def test_warden_reset_flag_is_404_for_an_unknown_file(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    answer = TestClient(app).post(
        "/api/watch/warden/flags/reset",
        json={"file_id": "nope", "stage": "format"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 404


def test_warden_reset_flag_is_409_while_a_pass_is_running(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [finished_record()])
    app.state.watch._running = True
    answer = TestClient(app).post(
        "/api/watch/warden/flags/reset",
        json={"file_id": "source", "stage": "format"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 409


def test_warden_reset_flag_needs_its_own_token(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seed(app.state.watch.home, [finished_record()])
    answer = TestClient(app).post(
        "/api/watch/warden/flags/reset",
        json={"file_id": "source", "stage": "format"})
    assert answer.status_code == 403


# --- run ---------------------------------------------------------------------

def test_warden_run_needs_the_watcher_configured_first(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    answer = TestClient(app).post(
        "/api/watch/warden/run", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 400


def test_warden_can_start_a_pass(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    ws = WatchSettings(folder_id="shared-folder", client_id="c", client_secret="s")
    ws.save(home)
    monkeypatch.setenv(ENV_VARS[GOOGLE_KEY], "refresh")

    gate, entered = threading.Event(), threading.Event()

    def slow(home, ws, **kw):
        entered.set()
        gate.wait(5)
        from app.watch.tick import TickReport
        return TickReport()

    app.state.watch._tick = slow
    answer = TestClient(app).post(
        "/api/watch/warden/run", headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200
    assert answer.json() == {"started": True}
    entered.wait(5)
    gate.set()
    app.state.watch.wait_idle()


# --- resend-completion ---------------------------------------------------------

def test_warden_resend_completion_is_404_with_no_job(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [finished_record()])       # no job_id set
    answer = TestClient(app).post(
        "/api/watch/warden/resend-completion",
        json={"file_id": "source"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 404


def test_warden_resend_completion_is_404_for_an_unknown_file(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    answer = TestClient(app).post(
        "/api/watch/warden/resend-completion",
        json={"file_id": "nope"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 404


def test_warden_resend_completion_finds_the_job_and_asks_notify(tmp_path, monkeypatch):
    monkeypatch.setenv(WARDEN_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [finished_record(job_id="job-1")])
    JobStore(Paths(home)).save(Job(
        id="job-1", filename="Aragon - Book 1.docx", source_path="src.docx",
        model="claude-haiku-4-5", mode="now", state="done"))

    answer = TestClient(app).post(
        "/api/watch/warden/resend-completion",
        json={"file_id": "source"},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["file_id"] == "source"
    assert body["job_id"] == "job-1"
    # Notifications are off by default, so nothing was actually sent — this
    # only proves the route found the right job and asked notify about it.
    assert body["sent"] is False
