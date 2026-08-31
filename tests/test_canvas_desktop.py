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

import pytest
from fastapi.testclient import TestClient

from app.canvas_desktop import LOCAL_KEY, build_shell_app


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
