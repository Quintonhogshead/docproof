"""The lane keys the portal sets, read by the agent: `/api/watch/agent-keys`.

The Galley agent runs on its own machine with its own volume, so a key an
administrator typed into the portal is on a disk it cannot see. This route is
the hop that closes that gap, and it is the third and last thing a bearer token
buys — so what it will and will not hand over is pinned down here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.main import create_app
from app.routes.watch import AGENT_KEY_PROVIDERS, AGENT_TOKEN_ENV
from app.settings import ENV_VARS, KEY_PROVIDERS, TYPESAFE, Paths
from app.watch.settings import GOOGLE_KEY

SECRET = "test-session-secret"
TOKEN = "an-agent-token-long-enough-to-be-a-secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for provider in KEY_PROVIDERS:
        monkeypatch.delenv(ENV_VARS[provider], raising=False)
    monkeypatch.delenv(ENV_VARS[GOOGLE_KEY], raising=False)
    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)


def make_app(tmp_path, *, web=True):
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    return create_app(tmp_path, start_runner=False, web=web,
                      session_secret=SECRET, https_only=False)


def get_keys(app, token=TOKEN):
    return TestClient(app).get("/api/watch/agent-keys",
                               headers={"Authorization": f"Bearer {token}"})


# --- what it answers ----------------------------------------------------------

def test_a_portal_key_reaches_the_agent_as_an_environment_value(tmp_path,
                                                                monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")

    answer = get_keys(app)
    assert answer.status_code == 200
    # Keyed by the environment variable the lane reads, not the provider name:
    # the agent copies this straight into the run environment.
    assert answer.json() == {"keys": {"TYPESAFE_API_KEY": "ts-live-secret"}}


def test_nothing_set_is_an_empty_set_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    answer = get_keys(app)
    assert answer.status_code == 200
    assert answer.json() == {"keys": {}}


def test_only_the_allow_listed_providers_are_served(tmp_path, monkeypatch):
    """A token that can claim a book cannot read the CRM's token or Drive's.

    The keystore holds every provider key the portal manages; this route hands
    back the lane keys and nothing else.
    """
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    for provider in KEY_PROVIDERS:
        app.state.keystore.set(provider, f"{provider}-secret")
    app.state.keystore.set(GOOGLE_KEY, "google-refresh-secret")

    keys = get_keys(app).json()["keys"]
    assert set(keys) == {ENV_VARS[p] for p in AGENT_KEY_PROVIDERS}
    assert "google-refresh-secret" not in keys.values()
    assert not any(name.startswith("GOOGLE") or name.startswith("HUBSPOT")
                   for name in keys)


def test_an_empty_stored_value_is_not_served(tmp_path, monkeypatch):
    """An empty string reads as "set" to a lane that only checks presence, so
    it never leaves here."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "")
    assert get_keys(app).json() == {"keys": {}}


def test_a_build_without_a_keystore_answers_empty(tmp_path, monkeypatch):
    """The desktop build has no portal and no keystore. An agent pointed at one
    gets an empty set and goes on with its own secrets, not a 500."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path, web=False)
    assert not hasattr(app.state, "keystore")
    answer = get_keys(app)
    assert answer.status_code == 200
    assert answer.json() == {"keys": {}}


# --- who may ask --------------------------------------------------------------

def test_the_wrong_token_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")
    assert get_keys(app, token="not-the-agent-token-but-long-enough").status_code == 401


def test_no_token_at_all_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")
    assert TestClient(app).get("/api/watch/agent-keys").status_code == 401


def test_a_server_with_no_agent_token_does_not_answer(tmp_path):
    """Same rule as the awaiting list: a server nobody configured for an agent
    answers no agent request, whatever is in the keystore."""
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")
    assert get_keys(app).status_code == 403


def test_a_browser_session_is_not_a_way_in(tmp_path, monkeypatch):
    """The key screen never reads a key back to a browser, and this route is
    not a hole in that: it takes the agent's bearer token, not a signed-in
    administrator's session."""
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")
    client = TestClient(app)
    client.post("/api/auth/login",
                json={"email": "boss@press.com", "password": "password1"})
    assert client.get("/api/watch/agent-keys").status_code == 401


# --- the value never reaches the log ------------------------------------------

def test_the_log_names_the_key_and_never_its_value(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(AGENT_TOKEN_ENV, TOKEN)
    app = make_app(tmp_path)
    app.state.keystore.set(TYPESAFE, "ts-live-secret")
    with caplog.at_level("INFO"):
        get_keys(app)
    assert "ts-live-secret" not in caplog.text
    assert "TYPESAFE_API_KEY" in caplog.text
