"""`docproof-serve` boot guards: it refuses to start without a session secret
or an API key, and otherwise builds the web app and hands it to uvicorn. We
stub uvicorn so no socket is ever opened."""
from __future__ import annotations

import pytest

from app import serve
from app.auth import SESSION_ENV


@pytest.fixture
def all_keys_off(monkeypatch):
    monkeypatch.setattr("app.serve.key_status",
                        lambda: {"anthropic": {"configured": False}})


@pytest.fixture
def one_key_on(monkeypatch):
    monkeypatch.setattr("app.serve.key_status",
                        lambda: {"anthropic": {"configured": True,
                                               "source": "environment"}})


def test_refuses_without_session_secret(monkeypatch, tmp_path, one_key_on):
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert serve.main(["--home", str(tmp_path)]) == 2


def test_refuses_without_any_api_key(monkeypatch, tmp_path, all_keys_off):
    monkeypatch.setenv(SESSION_ENV, "a-long-enough-secret")
    assert serve.main(["--home", str(tmp_path)]) == 2


def test_boots_the_web_app_when_configured(monkeypatch, tmp_path, one_key_on):
    monkeypatch.setenv(SESSION_ENV, "a-long-enough-secret")
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("app.serve.uvicorn.run", fake_run)
    rc = serve.main(["--home", str(tmp_path), "--port", "9999",
                     "--insecure-cookies"])
    assert rc == 0
    # It built the web build (the gate exists) and pointed uvicorn at 0.0.0.0.
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["port"] == 9999
    routes = {r.path for r in captured["app"].routes}
    assert "/api/login" in routes           # web build, not desktop
