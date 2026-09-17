"""The agent reading the portal's lane keys (galley/agent.py).

The key for an optional lane is typed into the web portal, which lives on a
different machine with a different volume, so the agent asks for it over the
same bearer-gated hop it polls for books on. The lane is optional in both
directions, and so is this: every way the ask can fail ends with the run going
ahead on whatever secrets the machine already holds.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from galley import agent as ga

APP = "https://atmosphere-docproof.fly.dev"
TOKEN = "s3cret-token-long-enough-to-be-real"
OAUTH = "sk-ant-oat-whatever"

ENV_TEXT = f"""export {ga.OAUTH_KEY}="{OAUTH}"
{ga.APP_URL_KEY}={APP}/
{ga.AGENT_TOKEN_KEY}='{TOKEN}'
"""


@pytest.fixture()
def env(tmp_path) -> ga.AgentEnv:
    path = tmp_path / "agent.env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    path.chmod(0o600)
    return ga.read_env(path)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeKeyApp:
    """The key route. Records what it was asked and answers what it was given."""

    def __init__(self, keys, *, status: int | None = None, body=None):
        self.keys = keys
        self.status = status
        self.body = body
        self.requests: list = []

    def __call__(self, request, timeout=30):
        self.requests.append(request)
        if self.status:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "no", {},
                io.BytesIO(b'{"detail":"Not the proofing agent."}'))
        if self.body is not None:
            return _Response(self.body)
        return _Response(json.dumps({"keys": self.keys}).encode("utf-8"))


# --- the ask ------------------------------------------------------------------

def test_the_keys_url_sits_beside_the_awaiting_url(env):
    assert env.keys_url == f"{APP}/api/watch/agent-keys"


def test_the_key_comes_back_as_an_environment_value(env):
    app = FakeKeyApp({"TYPESAFE_API_KEY": "ts-live-secret"})
    assert ga.fetch_portal_keys(env, opener=app) == {
        "TYPESAFE_API_KEY": "ts-live-secret"}
    # Asked as the agent, not as nobody.
    assert app.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_an_empty_value_is_dropped_rather_than_exported(env):
    """A lane that only checks for presence would read an empty string as a
    configured key and then fail its first call."""
    app = FakeKeyApp({"TYPESAFE_API_KEY": "", "OTHER_KEY": "   "})
    assert ga.fetch_portal_keys(env, opener=app) == {}


# --- every failure is survivable ----------------------------------------------

def test_an_older_server_without_the_route_is_not_an_error(env):
    assert ga.fetch_portal_keys(env, opener=FakeKeyApp({}, status=404)) == {}


def test_a_refusal_leaves_the_machines_own_secrets_alone(env):
    assert ga.fetch_portal_keys(env, opener=FakeKeyApp({}, status=401)) == {}


def test_an_unreachable_app_is_not_an_error(env):
    def boom(request, timeout=30):
        raise urllib.error.URLError("no route to host")

    assert ga.fetch_portal_keys(env, opener=boom) == {}


def test_an_answer_that_is_not_a_key_set_is_ignored(env):
    assert ga.fetch_portal_keys(env, opener=FakeKeyApp(None, body=b"[1, 2]")) == {}
    assert ga.fetch_portal_keys(
        env, opener=FakeKeyApp(None, body=b'{"keys": "nope"}')) == {}


def test_junk_that_is_not_json_is_ignored(env):
    assert ga.fetch_portal_keys(
        env, opener=FakeKeyApp(None, body=b"<html>502</html>")) == {}


# --- what the run is given ----------------------------------------------------

def _agent(env, tmp_path, **kw) -> ga.Agent:
    kw.setdefault("workspace_root", tmp_path / "ws")
    kw.setdefault("log", lambda _m: None)
    kw.setdefault("sleep", lambda _s: None)
    return ga.Agent(env=env, **kw)


def test_the_driver_environment_carries_the_portal_key(env, tmp_path):
    agent = _agent(env, tmp_path,
                   opener=FakeKeyApp({"TYPESAFE_API_KEY": "ts-live-secret"}))
    assert agent.driver_env()["TYPESAFE_API_KEY"] == "ts-live-secret"


def test_the_portal_key_wins_over_a_stale_one_on_this_machine(env, tmp_path,
                                                              monkeypatch):
    """The portal is where an administrator turns the lane on, so it is the
    newer intent — the same precedence the web build gives the same store."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "an-old-fly-secret")
    agent = _agent(env, tmp_path,
                   opener=FakeKeyApp({"TYPESAFE_API_KEY": "ts-live-secret"}))
    assert agent.driver_env()["TYPESAFE_API_KEY"] == "ts-live-secret"


def test_nothing_in_the_portal_keeps_this_machines_secret(env, tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "this-machines-own")
    agent = _agent(env, tmp_path, opener=FakeKeyApp({}))
    assert agent.driver_env()["TYPESAFE_API_KEY"] == "this-machines-own"


def test_a_failed_ask_never_stops_the_run(env, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "this-machines-own")
    agent = _agent(env, tmp_path, opener=FakeKeyApp({}, status=500))
    driver_env = agent.driver_env()
    assert driver_env["TYPESAFE_API_KEY"] == "this-machines-own"
    # And the rest of the environment is intact, not half-built.
    assert driver_env[ga.OAUTH_KEY] == OAUTH
