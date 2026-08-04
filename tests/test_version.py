"""Which build is this, and has the source moved on.

No test here runs git against the real repository: what the checkout happens to
be doing today is not something to assert on.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from app import version as versionlib
from docproof import __version__


def fake_git(answers: dict[str, str], missing: tuple[str, ...] = ()):
    """A git that answers by subcommand. Anything in `missing` fails the way
    git does — non-zero, empty stdout."""
    def run(command, **kwargs):
        sub = command[3]                        # git -C <dir> <subcommand> ...
        if sub in missing:
            return subprocess.CompletedProcess(command, 128, "", "fatal:")
        return subprocess.CompletedProcess(command, 0, answers.get(sub, ""), "")
    return run


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """A packaged .app: the spec stamped a build_info.json into it, and the
    source it was built from is somewhere else on disk."""
    source = tmp_path / "source"
    source.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "build_info.json").write_text(json.dumps({
        "version": "0.1.0", "built": "2026-08-04T12:00:00+00:00",
        "commit": "abc1234", "branch": "main", "source": str(source),
    }), encoding="utf-8")
    monkeypatch.setattr(versionlib, "resource_root", lambda: bundle)
    return source


# --- what this build is -------------------------------------------------------

def test_the_version_is_read_from_one_place():
    """pyproject, the .app bundle and the app all read docproof.__version__.
    If this ever needs changing in two places, that has gone wrong."""
    assert versionlib.build_info(runner=fake_git({}))["version"] == __version__


def test_a_packaged_build_says_when_and_from_what_it_was_built(frozen):
    info = versionlib.build_info(runner=fake_git({}))
    assert info["frozen"] is True
    assert info["commit"] == "abc1234"
    assert info["built"].startswith("2026-08-04")


def test_running_from_the_source_asks_git_instead(monkeypatch, tmp_path):
    monkeypatch.setattr(versionlib, "resource_root", lambda: tmp_path)
    info = versionlib.build_info(runner=fake_git(
        {"rev-parse": "deadbee"}))
    assert info["frozen"] is False and info["commit"] == "deadbee"
    assert info["built"] == ""            # nothing was built; this is the source


def test_a_corrupt_stamp_does_not_take_the_screen_down(monkeypatch, tmp_path):
    (tmp_path / "build_info.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(versionlib, "resource_root", lambda: tmp_path)
    assert versionlib.build_info(runner=fake_git({}))["version"] == __version__


def test_a_mac_with_no_git_still_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(versionlib, "resource_root", lambda: tmp_path)

    def no_git(command, **kwargs):
        raise FileNotFoundError("git")

    info = versionlib.build_info(runner=no_git)
    assert info["version"] == __version__ and info["commit"] == ""


# --- has anything moved on ----------------------------------------------------

def test_a_build_made_from_the_current_commit_is_up_to_date(frozen):
    r = versionlib.check_for_update(runner=fake_git({"rev-parse": "abc1234"}))
    assert r["ok"] and r["current"]
    assert "Up to date" in r["message"]


def test_a_build_left_behind_says_how_far_and_what_to_run(frozen):
    r = versionlib.check_for_update(
        runner=fake_git({"rev-parse": "999ffff", "rev-list": "3"}))
    assert r["ok"] and not r["current"]
    assert "3 changes" in r["message"] and "tools/update.sh" in r["message"]


def test_one_change_is_not_reported_as_1_changes(frozen):
    r = versionlib.check_for_update(
        runner=fake_git({"rev-parse": "999ffff", "rev-list": "1"}))
    assert "1 change in" in r["message"] and "There is 1 change" in r["message"]


def test_a_commit_that_is_no_longer_in_the_history_is_still_answerable(frozen):
    """A rebase, or a build from a branch since deleted. There is no count to
    give, but "different" is still the honest answer."""
    r = versionlib.check_for_update(
        runner=fake_git({"rev-parse": "999ffff"}, missing=("rev-list",)))
    assert r["ok"] and not r["current"] and "changes in" in r["message"]


def test_a_source_folder_that_has_gone_says_so(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "build_info.json").write_text(json.dumps({
        "version": "0.1.0", "built": "2026-08-04T12:00:00+00:00",
        "commit": "abc1234", "branch": "main",
        "source": "/Volumes/gone/docproof",
    }), encoding="utf-8")
    monkeypatch.setattr(versionlib, "resource_root", lambda: bundle)

    r = versionlib.check_for_update(runner=fake_git({}))
    assert not r["ok"] and "/Volumes/gone/docproof" in r["message"]


def test_a_source_folder_that_is_no_longer_a_checkout_says_so(frozen):
    r = versionlib.check_for_update(runner=fake_git({}, missing=("rev-parse",)))
    assert not r["ok"] and "not a git checkout" in r["message"]


def test_running_from_source_is_always_current(monkeypatch, tmp_path):
    monkeypatch.setattr(versionlib, "resource_root", lambda: tmp_path)
    r = versionlib.check_for_update(runner=fake_git({"rev-parse": "deadbee"}))
    assert r["ok"] and r["current"] and "as new as it gets" in r["message"]


def test_uncommitted_changes_are_mentioned_when_running_from_source(
        monkeypatch, tmp_path):
    monkeypatch.setattr(versionlib, "resource_root", lambda: tmp_path)
    r = versionlib.check_for_update(
        runner=fake_git({"rev-parse": "deadbee", "status": " M app/main.py"}))
    assert "uncommitted changes" in r["message"]
