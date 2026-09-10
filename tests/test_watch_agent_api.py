"""The one route a machine may read: `/api/watch/awaiting`.

The Mac (or Linux box) that holds the Claude subscription has no browser and no
session cookie, so the proofing agent reads this route with a bearer token
instead. Everything about that is deliberately small — read-only, one shape of
answer, and refused outright unless the server was given a token to check
against — and this is where the smallness is pinned down.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.main import create_app
from app.routes.watch import AGENT_TOKEN_ENV, MIN_AGENT_TOKEN
from app.settings import ENV_VARS, Paths
from app.watch import status as watchlib
from app.watch.settings import GOOGLE_KEY, WatchSettings
from app.watch.state import STATE_FILE, FileRecord, WatchState

SECRET = "test-session-secret"
TOKEN = "an-agent-token-long-enough-to-be-a-secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(ENV_VARS[GOOGLE_KEY], raising=False)
    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)


def make_app(tmp_path, *, web=True):
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    return create_app(tmp_path, start_runner=False, web=web,
                      session_secret=SECRET, https_only=False)


def seed(home, records):
    """A watcher whose state file holds these records."""
    ws = WatchSettings.load(home)
    ws.folder_id = "shared-folder"
    ws.save(home)
    state = WatchState(home / STATE_FILE)
    for rec in records:
        state.record(rec)
    return state


def awaiting_record(file_id="drive-1", name="Test - Book 1.docx", **kw):
    return FileRecord(file_id=file_id, name=name, proof_marked="awaiting",
                      author_last="Test", subfolder_id="folder-A",
                      subfolder_name="Test, A.", **kw)


# --- what it answers ----------------------------------------------------------

def test_the_agent_reads_the_awaiting_books(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [awaiting_record(),
                # Not awaiting: already finished, and never proofed at all.
                FileRecord(file_id="drive-2", name="Done - Book 1.docx",
                           proof_marked="done"),
                FileRecord(file_id="drive-3", name="Other - Book Original.docx")])

    answer = TestClient(app).get("/api/watch/awaiting",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200
    books = answer.json()["books"]
    assert len(books) == 1
    assert books[0]["file_id"] == "drive-1"
    assert books[0]["name"] == "Test - Book 1.docx"
    # Everything the agent needs to deliver the answer back: the author folder,
    # and the surname the workspace is named for.
    assert books[0]["folder_id"] == "folder-A"
    assert books[0]["subfolder_id"] == "folder-A"
    assert books[0]["author_last"] == "Test"


def test_a_flat_install_falls_back_to_the_watched_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home,
         [FileRecord(file_id="d1", name="Flat - Book 1.docx",
                     proof_marked="awaiting")])
    books = TestClient(app).get(
        "/api/watch/awaiting",
        headers={"Authorization": f"Bearer {TOKEN}"}).json()["books"]
    assert books[0]["folder_id"] == "shared-folder"
    assert books[0]["subfolder_id"] == ""


def test_the_answer_carries_no_settings(tmp_path, monkeypatch):
    """It is not the status payload. A bearer token buys the awaiting list and
    nothing else — no HubSpot values, no folder settings, no job history."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [awaiting_record()])
    payload = TestClient(app).get(
        "/api/watch/awaiting",
        headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert set(payload) == {"books"}
    assert set(payload["books"][0]) == {
        "file_id", "name", "folder_id", "subfolder_id", "author_last",
        "modified_time", "updated_at", "request_id"}


# --- who may read it ----------------------------------------------------------

def test_a_server_with_no_agent_token_answers_nobody(tmp_path):
    app = make_app(tmp_path)
    seed(app.state.watch.home, [awaiting_record()])
    answer = TestClient(app).get("/api/watch/awaiting",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 403
    assert AGENT_TOKEN_ENV in answer.json()["detail"]


def test_a_short_server_token_is_refused_as_not_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, "test")
    app = make_app(tmp_path)
    answer = TestClient(app).get("/api/watch/awaiting",
                                 headers={"Authorization": "Bearer test"})
    assert answer.status_code == 403
    assert str(MIN_AGENT_TOKEN) in answer.json()["detail"]


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token-but-also-long-enough-yes"},
    {"Authorization": TOKEN},                       # no scheme
    {"Authorization": f"Basic {TOKEN}"},            # wrong scheme
])
def test_anything_but_the_right_bearer_is_refused(tmp_path, monkeypatch,
                                                  headers):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    seed(app.state.watch.home, [awaiting_record()])
    answer = TestClient(app).get("/api/watch/awaiting", headers=headers)
    assert answer.status_code == 401
    assert "books" not in answer.json()


def test_the_route_is_read_only_and_the_rest_stays_shut(tmp_path, monkeypatch):
    """The token opens one GET. Everything else on the watch panel still needs
    an administrator's session."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    client = TestClient(app)
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/watch/awaiting", headers=bearer).status_code == 200
    assert client.get("/api/watch", headers=bearer).status_code == 401
    assert client.put("/api/watch", headers=bearer,
                      json={"folder": "x"}).status_code == 401
    assert client.post("/api/watch/run", headers=bearer).status_code == 401
    # …and the route itself takes no writes.
    assert client.post("/api/watch/awaiting", headers=bearer
                       ).status_code in (401, 405)


def test_the_desktop_build_still_wants_the_token(tmp_path, monkeypatch):
    """No session gate on the desktop build, but the agent route is not a
    door: a machine still has to present the secret."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path, web=False)
    seed(app.state.watch.home, [awaiting_record()])
    client = TestClient(app)
    assert client.get("/api/watch/awaiting").status_code == 401
    assert client.get("/api/watch/awaiting",
                      headers={"Authorization": f"Bearer {TOKEN}"}
                      ).status_code == 200


# --- the library function underneath ------------------------------------------

def test_awaiting_lists_only_awaiting_records(tmp_path):
    home = tmp_path / "watch"
    home.mkdir()
    seed(home, [awaiting_record(),
                awaiting_record(file_id="d2", name="Two - Book 1.docx"),
                FileRecord(file_id="d3", name="Three.docx",
                           proof_marked="failed")])
    books = watchlib.awaiting(home)
    assert [b["file_id"] for b in books] == ["drive-1", "d2"]


def test_awaiting_on_a_watcher_that_has_seen_nothing(tmp_path):
    home = tmp_path / "watch"
    home.mkdir()
    assert watchlib.awaiting(home) == []


def test_the_status_rows_carry_the_folder_ids_too(tmp_path):
    """The panel and the agent read the same record; `status` gained the two
    ids so a row can say which author's folder a book is in."""
    home = tmp_path / "watch"
    home.mkdir()
    seed(home, [awaiting_record()])
    row = watchlib.status(home)["files"][0]
    assert row["subfolder_id"] == "folder-A"
    assert row["author_last"] == "Test"
    assert row["proof_marked"] == "awaiting"


# --- the heartbeat -------------------------------------------------------------

def _boss(app):
    c = TestClient(app)
    assert c.post("/api/login", json={"email": "boss@press.com",
                                      "password": "password1"}).status_code == 200
    return c


def test_the_agent_can_report_and_the_drawer_reads_it_back(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    beat = {"agent": "fly-agent-1", "state": "running", "book": "Test - Book 1.docx",
            "phase": "settle", "poll_interval_s": 300}
    answer = TestClient(app).post("/api/watch/agent", json=beat,
                                  headers={"Authorization": f"Bearer {TOKEN}"})
    assert answer.status_code == 200
    assert answer.json() == {"ok": True}

    seen = _boss(app).get("/api/watch").json()["watch"]["agent"]
    assert seen["agent"] == "fly-agent-1"
    assert seen["phase"] == "settle"
    assert seen["received_at"]
    assert seen["stale"] is False
    assert seen["age_s"] < 60


def test_a_silent_agent_shows_as_stale(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    watchlib.save_agent_status(app.state.watch.home,
                               {"agent": "old", "poll_interval_s": 300})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    seen = watchlib.agent_status(app.state.watch.home, now=later)
    assert seen["stale"] is True
    assert seen["age_s"] > 3500
    # Nothing reported at all is simply absent, not an error.
    assert watchlib.agent_status(tmp_path / "nowhere") is None


def test_the_heartbeat_needs_the_agent_token(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    # No server token: refused, and nothing is written.
    assert TestClient(app).post("/api/watch/agent", json={"a": 1}).status_code == 403
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    assert TestClient(app).post(
        "/api/watch/agent", json={"a": 1},
        headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert watchlib.agent_status(app.state.watch.home) is None
    # A browser session is not the agent either.
    assert _boss(app).post("/api/watch/agent", json={"a": 1}).status_code == 401


def test_the_heartbeat_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    c = TestClient(app)
    assert c.post("/api/watch/agent", json=["not", "an", "object"],
                  headers=auth).status_code == 400
    assert c.post("/api/watch/agent", content=b"{not json",
                  headers={**auth, "Content-Type": "application/json"}
                  ).status_code == 400
    assert c.post("/api/watch/agent", json={"pad": "x" * 20000},
                  headers=auth).status_code == 413
    assert watchlib.agent_status(app.state.watch.home) is None


# --- taking a book back --------------------------------------------------------

def test_an_admin_can_release_an_awaiting_book(tmp_path, monkeypatch):
    """A killed test run must not come back at the agent's next boot: releasing
    the book takes it off the awaiting list the agent resumes from."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [awaiting_record()])
    boss = _boss(app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert len(TestClient(app).get("/api/watch/awaiting",
                                   headers=auth).json()["books"]) == 1

    answer = boss.post("/api/watch/proof/release", json={"file_id": "drive-1"})
    assert answer.status_code == 200
    body = answer.json()
    assert body["released"] == "drive-1"
    assert body["name"] == "Test - Book 1.docx"
    assert body["drive_marked"] is False           # no Google sign-in here
    # Gone from the agent's list, and from the drawer's awaiting rows.
    assert TestClient(app).get("/api/watch/awaiting",
                               headers=auth).json()["books"] == []
    rows = [f for f in body["watch"]["files"] if f["file_id"] == "drive-1"]
    assert rows and rows[0]["proof_marked"] == "failed"
    # Releasing it twice is a 404, not a second write.
    assert boss.post("/api/watch/proof/release",
                     json={"file_id": "drive-1"}).status_code == 404
    assert boss.post("/api/watch/proof/release",
                     json={"file_id": "nope"}).status_code == 404


def test_release_is_for_administrators_only(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seed(app.state.watch.home, [awaiting_record()])
    # No session at all, and the agent's bearer token is not a session either.
    assert TestClient(app).post("/api/watch/proof/release",
                                json={"file_id": "drive-1"}).status_code == 401
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    assert TestClient(app).post(
        "/api/watch/proof/release", json={"file_id": "drive-1"},
        headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


# --- trying a failed manuscript again -------------------------------------------

def failed_record(file_id="drive-9", name="Test - Book Original.docx", **kw):
    return FileRecord(file_id=file_id, name=name, marked="failed", attempts=2,
                      job_id="j-9", **kw)


def test_an_admin_can_clear_a_failed_marker(tmp_path, monkeypatch):
    """The "Try again" button: the four marker properties leave the file in
    Drive and the record goes back to untried, so the next pass looks at it."""
    from app.watch.stages import AT_PROP, JOB_PROP, REASON_PROP, STATE_PROP
    from tests.fakes import drive_entry, fake_drive

    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [failed_record()])
    opener = fake_drive({"drive-9": drive_entry(
        "Test - Book Original.docx",
        props={STATE_PROP: "failed", JOB_PROP: "j-9", AT_PROP: "2026-09-01",
               REASON_PROP: "verification failed"})})
    monkeypatch.setattr("app.watch.drive._open_url", opener)
    monkeypatch.setattr("app.routes.watch._drive_token_or_none",
                        lambda home: "access-1")
    boss = _boss(app)

    answer = boss.post("/api/watch/clear", json={"file_id": "drive-9"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["cleared"] == "drive-9"
    assert body["name"] == "Test - Book Original.docx"
    assert set(body["removed"]) == {STATE_PROP, JOB_PROP, AT_PROP, REASON_PROP}
    assert opener.files["drive-9"]["appProperties"] == {}
    rows = [f for f in body["watch"]["files"] if f["file_id"] == "drive-9"]
    assert rows and rows[0]["marked"] == "" and rows[0]["attempts"] == 0
    # Cleared already, so a second click is a 404, not a second write.
    assert boss.post("/api/watch/clear",
                     json={"file_id": "drive-9"}).status_code == 404
    assert boss.post("/api/watch/clear",
                     json={"file_id": "nope"}).status_code == 404


def test_clear_needs_a_google_sign_in(tmp_path, monkeypatch):
    """No "state only" fallback here: the marker in Drive is what the pass
    reads, so a clear that cannot reach Drive changes nothing and says so."""
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [failed_record()])
    monkeypatch.setattr("app.routes.watch._drive_token_or_none",
                        lambda home: None)
    answer = _boss(app).post("/api/watch/clear", json={"file_id": "drive-9"})
    assert answer.status_code == 400
    assert "Sign in to Google" in answer.json()["detail"]
    assert WatchState.load(home / STATE_FILE).files["drive-9"].marked == "failed"


def test_clear_will_not_touch_a_formatted_book(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    home = app.state.watch.home
    seed(home, [FileRecord(file_id="drive-9", name="Done.docx",
                           marked="formatted")])
    monkeypatch.setattr("app.routes.watch._drive_token_or_none",
                        lambda home: "access-1")
    assert _boss(app).post("/api/watch/clear",
                           json={"file_id": "drive-9"}).status_code == 404
    assert WatchState.load(home / STATE_FILE).files["drive-9"].marked == \
        "formatted"


def test_clear_is_for_administrators_only(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    seed(app.state.watch.home, [failed_record()])
    assert TestClient(app).post("/api/watch/clear",
                                json={"file_id": "drive-9"}).status_code == 401
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    assert TestClient(app).post(
        "/api/watch/clear", json={"file_id": "drive-9"},
        headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
