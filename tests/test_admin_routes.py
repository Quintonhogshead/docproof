"""God Mode: the in-app admin routes for managing users and caps, and the spend
cap they set. An ordinary user is walled out of every admin route; an admin can
create and adjust accounts but can't lock themselves out; and a user at their
limit is refused a new review with a 402."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.jobs import Job
from app.main import CAP_ENV, create_app
from app.settings import Paths
from .conftest import FIXTURES

SECRET = "test-session-secret"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.get_api_key", lambda p: "test-key")
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    accounts.create_user("editor@press.com", "password1")
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret=SECRET, https_only=False)


def _as(app, email, password="password1"):
    c = TestClient(app)
    assert c.post("/api/login",
                  json={"email": email, "password": password}).status_code == 200
    return c


def _seed_job(app, owner_email, job_id, **extra):
    owner_id = app.state.accounts.get_by_email(owner_email).id
    extra.setdefault("state", "done")
    # Dated now, so it counts toward this calendar month's spend — which is the
    # window the cap is measured over.
    extra.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    app.state.store.save(Job(
        id=job_id, filename="book.docx", source_path="/tmp/book.docx",
        model="claude-sonnet-5", mode="now", owner_id=owner_id, **extra))


def _upload(client, name="simple.docx"):
    with (FIXTURES / name).open("rb") as fh:
        r = client.post("/api/files", files={"files": (name, fh)})
    return r.json()["files"][0]["id"]


# -- who may reach God Mode ----------------------------------------------------

def test_ordinary_user_is_refused_every_admin_route(app):
    e = _as(app, "editor@press.com")
    assert e.get("/api/admin/users").status_code == 403
    assert e.post("/api/admin/users",
                  json={"email": "x@press.com", "password": "password1"}
                  ).status_code == 403
    assert e.get("/api/admin/usage").status_code == 403


def test_admin_routes_need_a_session_at_all(app):
    anon = TestClient(app)
    assert anon.get("/api/admin/users").status_code == 401


def test_desktop_build_has_no_admin_routes(tmp_path):
    app = create_app(tmp_path, start_runner=False)   # web=False
    with TestClient(app) as c:
        assert c.get("/api/admin/users").status_code in (404, 405)


# -- managing users ------------------------------------------------------------

def test_admin_lists_and_creates_users(app):
    boss = _as(app, "boss@press.com")
    listed = boss.get("/api/admin/users").json()["users"]
    assert {u["email"] for u in listed} == {"boss@press.com", "editor@press.com"}

    r = boss.post("/api/admin/users",
                  json={"email": "new@press.com", "password": "password1",
                        "monthly_cap": 15.0})
    assert r.status_code == 200 and r.json()["monthly_cap"] == 15.0
    # And the new user can now sign in.
    assert _as(app, "new@press.com").get("/api/me").status_code == 200


def test_admin_adjusts_cap_and_disables(app):
    boss = _as(app, "boss@press.com")
    uid = app.state.accounts.get_by_email("editor@press.com").id
    assert boss.put(f"/api/admin/users/{uid}",
                    json={"monthly_cap": 5.0}).json()["monthly_cap"] == 5.0
    # Clearing the cap (null) is distinct from leaving it alone.
    assert boss.put(f"/api/admin/users/{uid}",
                    json={"monthly_cap": None}).json()["monthly_cap"] is None
    boss.put(f"/api/admin/users/{uid}", json={"disabled": True})
    assert app.state.accounts.get_by_email("editor@press.com").disabled


def test_admin_cannot_lock_themselves_out(app):
    boss = _as(app, "boss@press.com")
    bid = app.state.accounts.get_by_email("boss@press.com").id
    assert boss.put(f"/api/admin/users/{bid}",
                    json={"disabled": True}).status_code == 400
    assert boss.put(f"/api/admin/users/{bid}",
                    json={"is_admin": False}).status_code == 400


def test_admin_usage_shows_every_user(app):
    _seed_job(app, "editor@press.com", "j1", cost=7.0, api_calls=1)
    boss = _as(app, "boss@press.com")
    rows = {r["email"]: r for r in boss.get("/api/admin/usage").json()["users"]}
    assert rows["editor@press.com"]["cost"] == pytest.approx(7.0)


# -- the spend cap ------------------------------------------------------------

def test_user_over_cap_is_refused(app):
    # Editor's cap is $5; they've already spent $6 this month.
    uid = app.state.accounts.get_by_email("editor@press.com").id
    app.state.accounts.set_cap(uid, 5.0)
    _seed_job(app, "editor@press.com", "spent", cost=6.0, api_calls=1)
    editor = _as(app, "editor@press.com")
    file_id = _upload(editor)
    r = editor.post("/api/jobs", json={"file_ids": [file_id],
                                       "model": "claude-sonnet-5", "mode": "batch"})
    assert r.status_code == 402
    assert "monthly limit" in r.json()["detail"]


def test_admin_is_never_capped(app, monkeypatch):
    # Even with a tiny server-wide default, the admin is exempt.
    monkeypatch.setenv(CAP_ENV, "0.01")
    _seed_job(app, "boss@press.com", "spent", cost=100.0, api_calls=1)
    boss = _as(app, "boss@press.com")
    file_id = _upload(boss)
    r = boss.post("/api/jobs", json={"file_ids": [file_id],
                                     "model": "claude-sonnet-5", "mode": "batch"})
    assert r.status_code == 200


def test_default_cap_applies_when_user_has_none(app, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "10.0")
    _seed_job(app, "editor@press.com", "spent", cost=11.0, api_calls=1)
    editor = _as(app, "editor@press.com")
    file_id = _upload(editor)
    r = editor.post("/api/jobs", json={"file_ids": [file_id],
                                       "model": "claude-sonnet-5", "mode": "batch"})
    assert r.status_code == 402


# -- review defaults (the web-relevant Settings options) ----------------------

def test_admin_sets_review_defaults(app):
    boss = _as(app, "boss@press.com")
    r = boss.put("/api/settings", json={"comments": False, "explanations": False})
    assert r.status_code == 200
    got = boss.get("/api/settings").json()["settings"]
    assert got["comments"] is False and got["explanations"] is False


def test_non_admin_cannot_change_settings(app):
    ed = _as(app, "editor@press.com")
    assert ed.put("/api/settings", json={"comments": False}).status_code == 403
    # Reading is fine — the Drop screen reads defaults for everyone.
    assert ed.get("/api/settings").status_code == 200


def test_desktop_settings_need_no_admin(tmp_path):
    app = create_app(tmp_path, start_runner=False)   # web=False
    with TestClient(app) as c:
        assert c.put("/api/settings",
                     json={"explanations": False}).status_code == 200
