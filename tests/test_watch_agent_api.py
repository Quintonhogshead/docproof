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
        "modified_time", "updated_at"}


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
