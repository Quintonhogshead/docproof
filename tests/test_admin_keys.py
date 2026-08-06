"""God Mode over provider API keys: an admin sets them in the portal, they take
effect for reviews, they persist and override the environment, and removing one
falls back to the environment. Non-admins can't touch any of it. Also: on the
web build, editing the detection prompts is an admin-only action.

The `clean_env` fixture wipes the provider env vars around each test so the
app's os.environ writes never leak between tests or to the real machine."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.keystore import KeyStore
from app.main import create_app
from app.settings import ENV_VARS, get_api_key
from app.settings import Paths

SECRET = "test-session-secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Recording each provider var with monkeypatch means its teardown restores
    # it — undoing any os.environ[...] = key the app did during the test.
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    # The server has no Keychain, so keys there come only from the environment
    # or the portal. The dev Mac running these tests does have one — silence it,
    # or a real stored key makes "not configured" look configured.
    import keyring
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)


def make_app(tmp_path):
    accounts = Accounts(Paths(tmp_path).users_db)
    accounts.create_user("boss@press.com", "password1", is_admin=True)
    accounts.create_user("ed@press.com", "password1")
    return create_app(tmp_path, start_runner=False, web=True,
                      session_secret=SECRET, https_only=False)


def _as(app, email):
    c = TestClient(app)
    assert c.post("/api/login",
                  json={"email": email, "password": "password1"}).status_code == 200
    return c


def test_admin_sets_key_and_reviews_use_it(tmp_path):
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    r = boss.put("/api/admin/keys/anthropic", json={"key": "sk-ant-portal"})
    assert r.status_code == 200
    # get_api_key (which every review uses) now returns it.
    assert get_api_key("anthropic") == "sk-ant-portal"
    row = next(k for k in r.json()["keys"] if k["provider"] == "anthropic")
    assert row["configured"] and row["source"] == "portal"


def test_key_persists_on_the_volume(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")
    boss.put("/api/admin/keys/openai", json={"key": "sk-openai-x"})
    assert KeyStore(Paths(tmp_path).keys_db).get("openai") == "sk-openai-x"


def test_removing_a_portal_key_reverts_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VARS["anthropic"], "sk-ant-fromenv")
    app = make_app(tmp_path)                 # env_keys snapshots the env value
    boss = _as(app, "boss@press.com")
    boss.put("/api/admin/keys/anthropic", json={"key": "sk-ant-portal"})
    assert get_api_key("anthropic") == "sk-ant-portal"
    boss.delete("/api/admin/keys/anthropic")
    # Back to the environment secret, not gone.
    assert get_api_key("anthropic") == "sk-ant-fromenv"


def test_empty_key_is_refused(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")
    assert boss.put("/api/admin/keys/anthropic", json={"key": "  "}).status_code == 400


def test_unknown_provider_is_404(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")
    assert boss.put("/api/admin/keys/parrot", json={"key": "x"}).status_code == 404


def test_non_admin_cannot_touch_keys(tmp_path):
    app = make_app(tmp_path)
    ed = _as(app, "ed@press.com")
    assert ed.get("/api/admin/keys").status_code == 403
    assert ed.put("/api/admin/keys/anthropic", json={"key": "x"}).status_code == 403
    assert ed.delete("/api/admin/keys/anthropic").status_code == 403


def test_key_status_reports_environment_source(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VARS["gemini"], "AIza-env")
    app = make_app(tmp_path)
    boss = _as(app, "boss@press.com")
    rows = {k["provider"]: k for k in boss.get("/api/admin/keys").json()["keys"]}
    assert rows["gemini"]["configured"] and rows["gemini"]["source"] == "environment"
    assert rows["anthropic"]["configured"] is False


# -- prompt editing is admin-only on the web build ----------------------------

def test_non_admin_cannot_edit_prompts(tmp_path):
    ed = _as(make_app(tmp_path), "ed@press.com")
    # 403 comes from the gate before the body is even looked at.
    r = ed.put("/api/prompts/comma_splice", json={"detection_prompt": "x"})
    assert r.status_code == 403


def test_admin_can_edit_prompts(tmp_path):
    boss = _as(make_app(tmp_path), "boss@press.com")
    key = boss.get("/api/prompts").json()["types"][0]["key"]
    r = boss.put(f"/api/prompts/{key}",
                 json={"detection_prompt": "Flag anything at all, for this test."})
    assert r.status_code == 200


def test_desktop_prompts_need_no_admin(tmp_path):
    app = create_app(tmp_path, start_runner=False)   # web=False
    with TestClient(app) as c:
        key = c.get("/api/prompts").json()["types"][0]["key"]
        assert c.put(f"/api/prompts/{key}",
                     json={"detection_prompt": "still works on desktop"}
                     ).status_code == 200
