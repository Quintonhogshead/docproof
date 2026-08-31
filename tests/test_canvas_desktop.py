"""The Mac shell's app wiring (app/canvas_desktop.py).

One regression owns this file: create_app mounts the static tree at "/" as
its last act, and a catch-all mount answers before any route registered
after it. The shell adds Cover Studio's routes after create_app returns, so
without the re-sort in build_shell_app the picker's /api/cover/jobs — and
every other cover route — 404ed out of the packaged .app while every
earlier-registered route worked, which is exactly the kind of failure that
survives a test suite that never builds the app the way the shell does.
"""
from __future__ import annotations

import inspect
import os

import pytest
from fastapi.testclient import TestClient

from app import desktop
from app.canvas_desktop import LOCAL_KEY, build_shell_app, cover_env_defaults


@pytest.fixture
def shell_client(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_KEY", LOCAL_KEY)
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path / "jobs"))
    app = build_shell_app(tmp_path / "home")
    with TestClient(app) as client:
        yield client


def test_cover_routes_are_reachable_past_the_root_static_mount(shell_client):
    answer = shell_client.get("/api/cover/jobs",
                              headers={"X-Cover-Key": LOCAL_KEY})
    assert answer.status_code == 200
    assert answer.json() == {"jobs": []}


def test_canvas_routes_still_answer_on_the_shell(shell_client):
    assert shell_client.get("/api/canvas/fonts.css").status_code == 200


def test_the_static_tree_still_serves_after_the_resort(shell_client):
    # The root mount was moved, not lost: the SPA itself still has to come
    # out of it or the shell opens a window onto a 404.
    answer = shell_client.get("/canvas", follow_redirects=True)
    assert answer.status_code == 200
    assert b"Cover Canvas" in answer.content


def test_the_main_app_carries_cover_studio_too(tmp_path, monkeypatch):
    # One press, one registration: cover routes now ride routes.register on
    # every build, gated by COVER_KEY — reachable when the key is set, and
    # honestly inert (503, not 404) when it is not.
    from app.main import create_app
    monkeypatch.setenv("COVER_KEY", LOCAL_KEY)
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path / "jobs"))
    with TestClient(create_app(tmp_path / "home", start_runner=False)) as c:
        assert c.get("/api/cover/jobs",
                     headers={"X-Cover-Key": LOCAL_KEY}).status_code == 200
    monkeypatch.delenv("COVER_KEY")
    with TestClient(create_app(tmp_path / "home2", start_runner=False)) as c:
        assert c.get("/api/cover/jobs").status_code == 503


# -- the environment both Mac shells assume -----------------------------------

def test_the_shell_defaults_cover_studio_onto_the_subscription(tmp_path,
                                                               monkeypatch):
    # On the owner's own machine the Claude login is the whole point: a
    # silent fall back to an API key is the "credit balance is too low"
    # failure this lane exists to prevent.
    for name in ("COVER_KEY", "COVER_DATA_PATH", "COVER_ANTHROPIC_LANE"):
        monkeypatch.delenv(name, raising=False)
    cover_env_defaults(tmp_path)
    assert os.environ["COVER_ANTHROPIC_LANE"] == "subscription"
    assert os.environ["COVER_KEY"] == LOCAL_KEY
    assert os.environ["COVER_DATA_PATH"] == str(tmp_path / "cover_jobs")


def test_the_shell_never_overrides_an_environment_that_named_one(tmp_path,
                                                                 monkeypatch):
    # Defaults, not pins: a deployment (or an owner who wants to spend
    # credits today) keeps what it set.
    monkeypatch.setenv("COVER_ANTHROPIC_LANE", "api")
    monkeypatch.setenv("COVER_KEY", "a-real-key")
    monkeypatch.setenv("COVER_DATA_PATH", str(tmp_path / "elsewhere"))
    cover_env_defaults(tmp_path)
    assert os.environ["COVER_ANTHROPIC_LANE"] == "api"
    assert os.environ["COVER_KEY"] == "a-real-key"
    assert os.environ["COVER_DATA_PATH"] == str(tmp_path / "elsewhere")


def test_the_token_file_fills_the_env_for_windowed_launches(tmp_path,
                                                            monkeypatch):
    # A Finder-launched .app inherits no shell export, and a headless claude
    # spawn does not see the interactive login — the claude_token file in
    # the app home is the one path a windowed launch has to the
    # subscription.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    (tmp_path / "claude_token").write_text("  sk-ant-oat-test-123  \n")
    cover_env_defaults(tmp_path)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test-123"


def test_no_token_file_leaves_the_env_silent(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cover_env_defaults(tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_an_exported_token_beats_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-the-shell")
    (tmp_path / "claude_token").write_text("from-the-file")
    cover_env_defaults(tmp_path)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "from-the-shell"


def test_the_docproof_window_assumes_the_same_environment():
    # One app, one press: the DocProof window sets these from the SAME
    # function rather than from a copy of it, which is the only thing that
    # keeps the two shells from drifting on what a local cover run does.
    # Read off main()'s source because main() opens a native window -- there
    # is nothing here to call.
    assert "cover_env_defaults(root)" in inspect.getsource(desktop.main)
