"""The web build's front door: every /api route needs a session, login/logout
work, the desktop build stays open, and the login form can't be hammered.
No vendor is reached — the runner is off; these tests are about the gate."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.auth import SESSION_ENV
from app.main import create_app
from app.settings import Paths


SECRET = "test-session-secret-not-for-production"


@pytest.fixture
def web(tmp_path):
    """A web-mode app with one ordinary user and one admin already created.
    https_only is off so TestClient's http cookies stick."""
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("editor@press.com", "password1")
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    app = create_app(tmp_path, start_runner=False, web=True,
                     session_secret=SECRET, https_only=False)
    with TestClient(app) as c:
        yield c


def _login(client, email="editor@press.com", password="password1"):
    return client.post("/api/login", json={"email": email, "password": password})


def test_api_is_closed_until_login(web):
    assert web.get("/api/jobs").status_code == 401
    assert web.get("/api/me").status_code == 401
    assert web.get("/api/settings").status_code == 401


def test_login_then_reach_api(web):
    r = _login(web)
    assert r.status_code == 200
    assert r.json()["email"] == "editor@press.com"
    assert r.json()["is_admin"] is False
    assert web.get("/api/jobs").status_code == 200
    assert web.get("/api/me").json()["email"] == "editor@press.com"


def test_wrong_password_is_refused(web):
    assert _login(web, password="nope").status_code == 401
    assert web.get("/api/jobs").status_code == 401


def test_unknown_email_is_refused_the_same_way(web):
    r = _login(web, email="ghost@press.com")
    assert r.status_code == 401
    assert r.json()["detail"] == "Wrong email or password."


def test_logout_closes_the_session(web):
    _login(web)
    assert web.get("/api/jobs").status_code == 200
    web.post("/api/logout")
    assert web.get("/api/jobs").status_code == 401


def test_disabled_user_loses_access_mid_session(web, tmp_path):
    _login(web)
    assert web.get("/api/me").status_code == 200
    # An admin disables them out from under the live cookie.
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.set_disabled(accounts.get_by_email("editor@press.com").id, True)
    assert web.get("/api/me").status_code == 401


def test_login_throttles_after_repeated_failures(web):
    for _ in range(5):
        assert _login(web, password="wrong").status_code == 401
    # Sixth attempt is locked out even though nothing new is being tried.
    assert _login(web, password="wrong").status_code == 429


def test_health_check_needs_no_session(web):
    assert web.get("/healthz").status_code == 200


def test_admin_flag_surfaces_for_god_mode(web):
    assert _login(web, email="boss@press.com").json()["is_admin"] is True


def test_missing_secret_refuses_to_build(tmp_path, monkeypatch):
    monkeypatch.delenv(SESSION_ENV, raising=False)
    with pytest.raises(RuntimeError, match="session secret"):
        create_app(tmp_path, start_runner=False, web=True, https_only=False)


def test_desktop_build_stays_open(tmp_path):
    """web=False is the unchanged desktop app: no login, no gate."""
    app = create_app(tmp_path, start_runner=False)
    with TestClient(app) as c:
        assert c.get("/api/jobs").status_code == 200   # no session required
        # No login handler exists in the desktop build (the static mount, not a
        # route, is what answers here).
        assert c.post("/api/login", json={}).status_code != 200
