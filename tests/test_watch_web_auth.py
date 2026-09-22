"""DocWatch's Google sign-in on the hosted build, over HTTP.

The desktop app opens a browser on the machine and catches Google's answer on a
loopback listener (test_watch_app.py covers that). A Linux server can do
neither, so the web build sends the browser to Google itself and Google returns
the answer to `/api/watch/auth/callback`. What this file pins down:

- the button hands back a consent page to go to, carrying a state to check;
- the callback trades the code for a refresh token and keeps it the web way —
  in the volume's keystore and live in the environment, not the Mac Keychain;
- a state that does not match is refused and stores nothing;
- a token and the client that minted it are written to the environment as one
  piece, so a panel sign-in is never left beside an older fly-secret client —
  the pair Google reads as a revoked sign-in;
- forgetting clears both places, and falls back to the boot env sign-in;
- the whole panel is admin-only, and a token kept in the keystore is loaded
  back into the environment at boot.

The `clean_env` fixture wipes the Google env var and silences the dev Mac's
Keychain, the same discipline test_admin_keys.py keeps.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.keystore import KeyStore
from app.main import create_app, watch_home_for
from app.settings import ENV_VARS, Paths
from app.watch import auth as authlib
from app.watch.settings import (CLIENT_ID_ENV, CLIENT_SECRET_ENV, GOOGLE_KEY,
                                WatchSettings, google_client)

SECRET = "test-session-secret"
GOOGLE_ENV = ENV_VARS[GOOGLE_KEY]


GOOGLE_VARS = (GOOGLE_ENV, CLIENT_ID_ENV, CLIENT_SECRET_ENV)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Snapshot and put back, rather than `monkeypatch.delenv`.

    The app writes these three directly — boot and the callback both go through
    `set_google_environment` — and `delenv` on a name that was never set
    records nothing to undo, so a variable the boot then set would leak into
    every test file that ran after this one."""
    import os
    before = {name: os.environ.get(name) for name in GOOGLE_VARS}
    for name in GOOGLE_VARS:
        os.environ.pop(name, None)
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    # The button asks Google whether it will take the request before sending
    # the browser there. Not from a test: Google says nothing here.
    monkeypatch.setattr(authlib, "preflight_consent", lambda url, **k: None)
    yield
    for name, value in before.items():
        os.environ.pop(name, None)
        if value is not None:
            os.environ[name] = value


def fly_secrets(monkeypatch, client_id="fly-id", client_secret="fly-secret",
                token="from-fly-secret"):
    """The three Google secrets a deployment is given, set together.

    Straight into the environment, because `clean_env` is what puts it back."""
    import os
    os.environ[CLIENT_ID_ENV] = client_id
    os.environ[CLIENT_SECRET_ENV] = client_secret
    os.environ[GOOGLE_ENV] = token


def make_app(tmp_path):
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    accounts.create_user("ed@press.com", "password1")
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret=SECRET, https_only=False)


def _as(app, email):
    c = TestClient(app)
    assert c.post("/api/login",
                  json={"email": email, "password": "password1"}
                  ).status_code == 200
    return c


def with_client(app):
    """Give the watcher a saved OAuth client, the way a first sign-in would."""
    ws = WatchSettings.load(app.state.watch.home)
    ws.client_id, ws.client_secret = "web-id", "web-secret"
    ws.save(app.state.watch.home)


def state_in(consent_url: str) -> str:
    return parse_qs(urlparse(consent_url).query)["state"][0]


# --- starting the sign-in -----------------------------------------------------

def test_the_button_hands_back_a_consent_page_to_go_to(tmp_path):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")

    body = boss.post("/api/watch/auth",
                     json={"client_id": "web-id",
                           "client_secret": "web-secret"}).json()

    url = body["consent_url"]
    assert "accounts.google.com" in url
    # The redirect Google is handed points back at this server's callback, https
    # even behind the proxy, and carries the state the callback will check.
    assert "https%3A%2F%2Ftestserver%2Fapi%2Fwatch%2Fauth%2Fcallback" in url
    assert state_in(url)
    # The client is remembered so the bare GET from Google can finish with it.
    assert WatchSettings.load(app.state.watch.home).client_id == "web-id"
    assert app.state.watch_auth["state"] == state_in(url)


def test_signing_in_still_needs_an_oauth_client(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")

    answer = boss.post("/api/watch/auth", json={})

    assert answer.status_code == 400
    assert "Google Cloud console" in answer.json()["detail"]


# --- finishing at the callback ------------------------------------------------

def test_the_callback_keeps_the_token_and_sends_the_browser_back(tmp_path,
                                                                 monkeypatch):
    app = make_app(tmp_path)
    with_client(app)
    boss = _as(app, "boss@press.com")
    monkeypatch.setattr(authlib, "exchange_code",
                        lambda *a, **k: "refresh-token-xyz")

    state = state_in(boss.post("/api/watch/auth", json={}).json()["consent_url"])
    answer = boss.get(f"/api/watch/auth/callback?state={state}&code=one-time",
                      follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/#watch"
    # Kept the web way: the keystore on the volume and the live environment.
    assert KeyStore(Paths(tmp_path).keys_db).get(GOOGLE_KEY) == "refresh-token-xyz"
    import os
    assert os.environ[GOOGLE_ENV] == "refresh-token-xyz"
    # Single-use: the pending sign-in is spent.
    assert app.state.watch_auth is None
    assert boss.get("/api/watch").json()["watch"]["signed_in"] is True


def test_an_answer_whose_state_does_not_match_stores_nothing(tmp_path,
                                                             monkeypatch):
    app = make_app(tmp_path)
    with_client(app)
    boss = _as(app, "boss@press.com")
    exchanged = []
    monkeypatch.setattr(authlib, "exchange_code",
                        lambda *a, **k: exchanged.append(1) or "nope")

    boss.post("/api/watch/auth", json={})
    answer = boss.get("/api/watch/auth/callback?state=forged&code=one-time",
                      follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"].startswith("/?watch_auth=error")
    assert exchanged == []                       # never got as far as trading it
    assert KeyStore(Paths(tmp_path).keys_db).get(GOOGLE_KEY) is None
    assert app.state.watch_auth is None          # spent even on refusal


def test_a_callback_with_no_pending_sign_in_is_an_error_not_a_crash(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")

    answer = boss.get("/api/watch/auth/callback?state=x&code=y",
                      follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"].startswith("/?watch_auth=error")


# --- forgetting ---------------------------------------------------------------

def test_forgetting_clears_the_keystore_and_the_environment(tmp_path,
                                                            monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    app.state.keystore.set(GOOGLE_KEY, "stored-token")
    import os
    os.environ[GOOGLE_ENV] = "stored-token"

    boss.delete("/api/watch/auth")

    assert KeyStore(Paths(tmp_path).keys_db).get(GOOGLE_KEY) is None
    assert GOOGLE_ENV not in os.environ


def test_forgetting_puts_the_boot_sign_in_back_whole(tmp_path, monkeypatch):
    """The triple, not the token alone: a boot token restored beside the
    client the forgotten sign-in used is the mismatch this route cleans up."""
    fly_secrets(monkeypatch)
    app = make_app(tmp_path)                      # snapshots the boot env triple
    boss = _as(app, "boss@press.com")
    app.state.keystore.set(GOOGLE_KEY, "portal-token")
    import os
    os.environ[CLIENT_ID_ENV] = "portal-id"
    os.environ[CLIENT_SECRET_ENV] = "portal-secret"
    os.environ[GOOGLE_ENV] = "portal-token"

    boss.delete("/api/watch/auth")

    assert os.environ[GOOGLE_ENV] == "from-fly-secret"
    assert os.environ[CLIENT_ID_ENV] == "fly-id"
    assert os.environ[CLIENT_SECRET_ENV] == "fly-secret"


# --- boot and gating ----------------------------------------------------------

def test_a_stored_token_is_loaded_into_the_environment_at_boot(tmp_path,
                                                              monkeypatch):
    """With the client it was minted with, over the fly secrets' own.

    This is the September 2026 failure in one test: the keystore's token and
    `watch.json`'s client are one sign-in, and a redeploy that put the token
    back beside `GOOGLE_CLIENT_ID` made every refresh an `invalid_grant`."""
    fly_secrets(monkeypatch)
    KeyStore(Paths(tmp_path).keys_db).set(GOOGLE_KEY, "survives-redeploy")
    # The sign-in as the volume holds it after a redeploy: the token in the
    # keystore, the client it was minted with in watch.json.
    home = watch_home_for(Path(tmp_path))
    ws = WatchSettings.load(home)
    ws.client_id, ws.client_secret = "web-id", "web-secret"
    ws.save(home)

    app = make_app(tmp_path)

    import os
    assert os.environ[GOOGLE_ENV] == "survives-redeploy"
    assert os.environ[CLIENT_ID_ENV] == "web-id"
    assert os.environ[CLIENT_SECRET_ENV] == "web-secret"
    assert google_client(WatchSettings.load(app.state.watch.home)) == (
        "web-id", "web-secret")


def test_a_stored_token_with_no_client_is_left_out_of_the_environment(tmp_path,
                                                                     monkeypatch):
    """Unpairable is worse than absent: beside a fly-secret client it would be
    a sign-in that looks present and fails at Google every time."""
    fly_secrets(monkeypatch)
    KeyStore(Paths(tmp_path).keys_db).set(GOOGLE_KEY, "orphan-token")

    make_app(tmp_path)                            # no client in watch.json

    import os
    assert os.environ[GOOGLE_ENV] == "from-fly-secret"
    assert os.environ[CLIENT_ID_ENV] == "fly-id"


def test_a_panel_sign_in_does_not_inherit_the_fly_secret_client(tmp_path,
                                                                monkeypatch):
    """Signing in through the panel with one client used to leave the token
    beside another, which Google refuses exactly as it refuses a revoked
    sign-in — so the panel asked for a sign-in that could not help, forever."""
    fly_secrets(monkeypatch)
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    monkeypatch.setattr(authlib, "exchange_code", lambda *a, **k: "portal-token")

    url = boss.post("/api/watch/auth",
                    json={"client_id": "portal-id",
                          "client_secret": "portal-secret"}).json()["consent_url"]
    boss.get(f"/api/watch/auth/callback?state={state_in(url)}&code=one-time",
             follow_redirects=False)

    import os
    assert os.environ[GOOGLE_ENV] == "portal-token"
    assert os.environ[CLIENT_ID_ENV] == "portal-id"
    assert os.environ[CLIENT_SECRET_ENV] == "portal-secret"
    # And what every Drive call actually resolves: the pair, not one of each.
    assert google_client(WatchSettings.load(app.state.watch.home)) == (
        "portal-id", "portal-secret")


def test_docwatch_is_admin_only_on_the_web(tmp_path):
    app = make_app(tmp_path)
    ed = _as(app, "ed@press.com")

    assert ed.get("/api/watch").status_code == 403
    assert ed.post("/api/watch/auth", json={}).status_code == 403
    assert ed.post("/api/watch/run").status_code == 403
    # The write too, which is what the Automations panel's workflow drawers
    # save through — the proofing switch and its HubSpot values included. An
    # editor can neither read the settings nor change what DocProof writes into
    # the CRM.
    assert ed.put("/api/watch", json={"proofing_enabled": True}).status_code \
        == 403


def test_the_desktop_sign_in_is_untouched(tmp_path):
    """The web branch is gated on app.state.web; the desktop app still opens a
    browser and reports through the poll, never a consent_url."""
    app = create_app(tmp_path, start_runner=False)   # web=False
    app.state.watch._sign_in = lambda h, cid, secret, **kw: None
    ws = WatchSettings.load(app.state.watch.home)
    ws.folder_id, ws.client_id, ws.client_secret = "f", "c", "s"
    ws.save(app.state.watch.home)

    with TestClient(app) as c:
        body = c.post("/api/watch/auth", json={}).json()

    # No consent page to go to — the desktop opens the browser itself and
    # reports through the sign_in state the poll reads, not a URL.
    assert "consent_url" not in body
    assert body["sign_in"] is not None


# --- Google refusing before the browser goes ----------------------------------

def test_a_client_google_would_refuse_is_refused_here_with_the_address(
        tmp_path, monkeypatch):
    """Google's "redirect_uri_mismatch" page never says which address it
    wanted. The panel checks first and says exactly what to add."""
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    seen = []
    monkeypatch.setattr(authlib, "preflight_consent",
                        lambda url, **k: seen.append(url) or "redirect_uri_mismatch")

    answer = boss.post("/api/watch/auth",
                       json={"client_id": "desktop-id",
                             "client_secret": "desktop-secret"})

    assert answer.status_code == 400
    detail = answer.json()["detail"]
    assert "https://testserver/api/watch/auth/callback" in detail
    assert "authorized redirect URI" in detail
    assert seen and "accounts.google.com" in seen[0]
    # Nothing was saved and nothing is pending: the next try starts clean.
    assert WatchSettings.load(app.state.watch.home).client_id == ""
    assert app.state.watch_auth is None


def test_the_fly_secret_client_is_named_as_the_macs_desktop_client(
        tmp_path, monkeypatch):
    fly_secrets(monkeypatch, client_id="mac-desktop-id")
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    monkeypatch.setattr(authlib, "preflight_consent",
                        lambda url, **k: "redirect_uri_mismatch")

    detail = boss.post("/api/watch/auth",
                       json={"client_id": "mac-desktop-id",
                             "client_secret": "s"}).json()["detail"]

    assert "Desktop" in detail and "Web application" in detail


def test_the_panel_is_told_the_exact_redirect_to_allow(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")

    body = boss.get("/api/watch").json()

    assert body["redirect_uri"] == "https://testserver/api/watch/auth/callback"


def test_a_preflight_google_cannot_answer_does_not_block(tmp_path,
                                                          monkeypatch):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    monkeypatch.setattr(authlib, "preflight_consent", lambda url, **k: None)

    body = boss.post("/api/watch/auth",
                     json={"client_id": "web-id",
                           "client_secret": "web-secret"}).json()

    assert "consent_url" in body
