"""Which build is this, and has the source moved on.

No test here runs git against the real repository: what the checkout happens to
be doing today is not something to assert on.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error

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


def _stamp(bundle, **overrides):
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "build_info.json").write_text(json.dumps({
        "version": "0.1.0", "built": "2026-08-04T12:00:00+00:00",
        "commit": "abc1234", "branch": "main", "repo": "acme/docproof",
        **overrides,
    }), encoding="utf-8")
    return bundle


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """A packaged .app on the machine that built it: the source it came from is
    still there, so the checkout is the thing to compare against."""
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(versionlib, "resource_root",
                        lambda: _stamp(tmp_path / "bundle", source=str(source)))
    return source


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """A build somebody was sent: no checkout anywhere, so the published
    releases are the only thing it can ask about."""
    monkeypatch.setattr(versionlib, "resource_root",
                        lambda: _stamp(tmp_path / "bundle",
                                       source="/Volumes/someone-elses/docproof"))
    monkeypatch.setattr(versionlib, "get_api_key", lambda name: "tok_123")
    return tmp_path


def fake_github(payload, *, status=None, downloads=b""):
    """Stands in for urllib. Records what was asked for, so the tests can check
    the token was sent and the right asset requested."""
    calls = []

    class Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request, timeout=30):
        calls.append(request)
        if status is not None:
            raise urllib.error.HTTPError(request.full_url, status, "nope",
                                         {}, None)
        if request.get_header("Accept") == "application/octet-stream":
            return Response(downloads)
        return Response(json.dumps(payload).encode())

    opener.calls = calls
    return opener


RELEASE = {
    "tag_name": "v0.2.0", "name": "DocProof 0.2.0", "body": "Notes here.",
    "assets": [{"id": 99, "name": "DocProof-0.2.0-abc1234.dmg",
                "size": 45_000_000}],
}


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
        {"rev-parse": "deadbee", "remote": "git@github.com:acme/docproof.git"}))
    assert info["frozen"] is False and info["commit"] == "deadbee"
    assert info["built"] == ""            # nothing was built; this is the source
    assert info["repo"] == "acme/docproof"


@pytest.mark.parametrize("url, slug", [
    ("git@github.com:acme/docproof.git", "acme/docproof"),
    ("https://github.com/acme/docproof.git", "acme/docproof"),
    ("https://github.com/acme/docproof", "acme/docproof"),
    ("ssh://git@github.com/acme/docproof.git", "acme/docproof"),
    ("https://gitlab.com/acme/docproof.git", ""),      # not somewhere we check
    ("", ""),
])
def test_the_repo_is_read_from_whatever_shape_the_remote_is(url, slug):
    assert versionlib.repo_slug(url) == slug


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


def test_a_build_left_behind_says_how_far_and_offers_to_rebuild(frozen):
    """The machine with the source can build the answer itself, so it says how
    far behind it is and nothing about typing a command."""
    r = versionlib.check_for_update(
        runner=fake_git({"rev-parse": "999ffff", "rev-list": "3"}))
    assert r["ok"] and not r["current"]
    assert "3 changes" in r["message"]
    assert r["can_rebuild"] is True
    assert "tools/update.sh" not in r["message"]


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


# --- a build somebody was sent ------------------------------------------------

def test_a_sent_build_asks_github_what_has_been_released(sent):
    github = fake_github(RELEASE)
    r = versionlib.check_for_update(runner=fake_git({}), opener=github)

    assert r["ok"] and not r["current"] and r["source"] == "github"
    assert "DocProof 0.2.0 is available" in r["message"]
    assert r["asset"]["name"].endswith(".dmg")
    # Asked the right repo, with the token, pinned to an API version.
    request = github.calls[0]
    assert request.full_url.endswith("/repos/acme/docproof/releases/latest")
    assert request.get_header("Authorization") == "Bearer tok_123"
    assert request.get_header("X-github-api-version")


def test_a_sent_build_on_the_newest_release_says_so(sent):
    r = versionlib.check_for_update(
        runner=fake_git({}), opener=fake_github({**RELEASE, "tag_name": "v0.1.0"}))
    assert r["ok"] and r["current"] and "latest release" in r["message"]


def test_without_a_token_it_says_what_to_do_rather_than_failing(sent,
                                                                monkeypatch):
    monkeypatch.setattr(versionlib, "get_api_key", lambda name: None)
    r = versionlib.check_for_update(runner=fake_git({}),
                                    opener=fake_github(RELEASE))
    assert not r["ok"] and r["needs_token"]
    assert "paste the GitHub token" in r["message"]


def test_a_token_github_will_not_accept_says_which_problem_it_is(sent):
    r = versionlib.check_for_update(runner=fake_git({}),
                                    opener=fake_github({}, status=401))
    assert not r["ok"] and "would not accept that token" in r["message"]


def test_a_repo_with_no_releases_reads_the_same_as_no_access(sent):
    """GitHub answers 404 both when a private repo is invisible to the token
    and when it simply has no release yet, so the message has to own that."""
    r = versionlib.check_for_update(runner=fake_git({}),
                                    opener=fake_github({}, status=404))
    assert not r["ok"]
    assert "none has been made yet" in r["message"]
    assert "cannot see that repository" in r["message"]


def test_being_offline_is_a_sentence_not_a_traceback(sent):
    def offline(request, timeout=30):
        raise urllib.error.URLError("no route to host")

    r = versionlib.check_for_update(runner=fake_git({}), opener=offline)
    assert not r["ok"] and "Could not reach GitHub" in r["message"]


def test_a_build_that_says_nothing_about_where_it_came_from(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(versionlib, "resource_root",
                        lambda: _stamp(tmp_path / "b", repo="", source="/gone"))
    r = versionlib.check_for_update(runner=fake_git({}))
    assert not r["ok"] and "ask whoever sent it" in r["message"].lower()


def test_a_release_with_no_disk_image_does_not_offer_a_download(sent):
    r = versionlib.check_for_update(
        runner=fake_git({}), opener=fake_github({**RELEASE, "assets": []}))
    assert r["ok"] and not r["current"] and r["asset"] is None
    assert "no disk image attached" in r["message"]


# --- comparing versions -------------------------------------------------------

@pytest.mark.parametrize("candidate, current, newer", [
    ("v0.2.0", "0.1.0", True),
    ("v0.1.0", "0.1.0", False),
    ("v0.1.0", "0.2.0", False),
    ("v0.10.0", "0.9.0", True),      # not a string comparison
    ("0.1.1", "0.1.0", True),
    ("v1.0", "0.9.9", True),
])
def test_which_version_is_newer(candidate, current, newer):
    assert versionlib.is_newer(candidate, current) is newer


# --- downloading --------------------------------------------------------------

def test_downloading_puts_the_disk_image_where_downloads_go(sent, tmp_path):
    github = fake_github(RELEASE, downloads=b"disk image bytes")
    into = tmp_path / "Downloads"

    r = versionlib.download_release(runner=fake_git({}), opener=github,
                                    into=into)
    assert r["ok"]
    written = into / "DocProof-0.2.0-abc1234.dmg"
    assert written.read_bytes() == b"disk image bytes"
    assert "Downloads folder" in r["message"]
    # Fetched by asset id, asking for the bytes rather than the description.
    asset_request = github.calls[-1]
    assert asset_request.full_url.endswith("/releases/assets/99")
    assert asset_request.get_header("Accept") == "application/octet-stream"


def test_downloading_when_there_is_nothing_newer_is_refused(sent, tmp_path):
    r = versionlib.download_release(
        runner=fake_git({}),
        opener=fake_github({**RELEASE, "tag_name": "v0.1.0"}),
        into=tmp_path)
    assert not r["ok"] and "Up to date" in r["message"]
    assert not list(tmp_path.glob("*.dmg"))


def test_a_download_that_breaks_off_says_so(sent, tmp_path):
    calls = {"n": 0}

    def flaky(request, timeout=30):
        calls["n"] += 1
        if request.get_header("Accept") == "application/octet-stream":
            raise urllib.error.URLError("connection reset")
        return fake_github(RELEASE)(request, timeout)

    r = versionlib.download_release(runner=fake_git({}), opener=flaky,
                                    into=tmp_path)
    assert not r["ok"] and "did not finish" in r["message"]


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
