"""Self-update, without ever updating anything.

No test here mounts a disk image, replaces a bundle, or exits a process — the
subprocess boundary, the terminator, and the frozen-ness of the build are all
injected. What is tested is the refusals (which matter more than the happy
path: they are what protect a running review and a still-working install) and
the order of operations around the swap.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import update as updatelib
from app.jobs import Job, JobRunner, JobStore
from app.settings import Paths, Settings
from app.update import UpdateError, perform_update, refuse_reason
from .conftest import FIXTURES

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


@pytest.fixture
def runner(tmp_path):
    paths = Paths(tmp_path).ensure()
    return JobRunner(JobStore(paths), Settings(), config_path=CONFIG)


def _frozen_at(monkeypatch, tmp_path, name="DocProof.app") -> Path:
    """Pretend this process lives in a packaged bundle at tmp_path/name."""
    bundle = tmp_path / "Applications" / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(bundle / "Contents" / "MacOS" / "DocProof"))
    return bundle


# --- the refusals -------------------------------------------------------------

def test_running_from_source_has_nothing_to_replace(runner):
    reason = refuse_reason(runner)
    assert "source" in reason and "rebuild" in reason


def test_a_translocated_app_is_told_to_install_first(runner, monkeypatch,
                                                     tmp_path):
    bundle = tmp_path / "AppTranslocation" / "ABC123" / "d" / "DocProof.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(bundle / "Contents" / "MacOS" / "DocProof"))
    assert "Drag it to Applications" in refuse_reason(runner)


def test_no_update_while_a_document_is_being_worked_on(runner, monkeypatch,
                                                       tmp_path):
    _frozen_at(monkeypatch, tmp_path)
    runner.store.save(Job(id="j1", filename="f.docx", source_path="/f.docx",
                          model="m", mode="now", state="running"))
    reason = refuse_reason(runner)
    assert "being worked on" in reason and "let it finish" in reason.lower()


def test_a_waiting_overnight_job_does_not_block_updating(runner, monkeypatch,
                                                         tmp_path):
    """A batch at the vendor survives any restart — the ticker finds it by its
    manifest. Only active local work blocks."""
    _frozen_at(monkeypatch, tmp_path)
    runner.store.save(Job(id="j1", filename="f.docx", source_path="/f.docx",
                          model="m", mode="batch", state="waiting"))
    assert refuse_reason(runner) is None


def test_refusals_happen_before_anything_is_downloaded(runner, monkeypatch):
    called = []
    monkeypatch.setattr(updatelib, "download_release",
                        lambda **k: called.append(1))
    with pytest.raises(UpdateError):
        perform_update(runner)               # from source → refused
    assert not called


# --- the swap -----------------------------------------------------------------

class SwapHarness:
    """Everything perform_update touches, faked at the boundaries."""

    def __init__(self, monkeypatch, tmp_path, runner, *, version="0.2.0"):
        self.bundle = _frozen_at(monkeypatch, tmp_path)
        (self.bundle / "Contents" / "old-marker").write_text("old")
        self.trash = tmp_path / ".Trash"
        self.trash.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        monkeypatch.setattr(updatelib, "check_for_update", lambda: {
            "ok": True, "current": False, "tag": f"v{version}",
            "release_name": f"DocProof {version}",
            "asset": {"name": "d.dmg", "size": 1}})

        def fake_download(into):
            dmg = into / "d.dmg"
            dmg.write_bytes(b"dmg")
            return {"ok": True, "path": str(dmg)}
        monkeypatch.setattr(updatelib, "download_release",
                            lambda into: fake_download(into))

        self.commands: list[list[str]] = []
        self.stamp_version = version
        harness = self

        def run(command, **kwargs):
            harness.commands.append(command)
            if command[0] == "hdiutil" and command[1] == "attach":
                mount = Path(command[2 if "attach" != command[1] else 3])
                mount = Path(command[command.index("-mountpoint") + 1])
                app = mount / "DocProof.app" / "Contents" / "Frameworks"
                app.mkdir(parents=True)
                (app / "build_info.json").write_text(json.dumps(
                    {"version": harness.stamp_version}))
            if command[0] == "cp":
                src, dst = Path(command[2]), Path(command[3])
                dst.mkdir(parents=True, exist_ok=True)
                for item in src.rglob("*"):
                    if item.is_file():
                        target = dst / item.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(item.read_bytes())
            return subprocess.CompletedProcess(command, 0, "", "")

        self.run = run
        self.spawned: list[list[str]] = []
        self.spawn = lambda cmd, **k: self.spawned.append(cmd)
        self.terminated = []
        self.terminate = lambda: self.terminated.append(True)
        self.runner = runner

    def update(self):
        return perform_update(self.runner, run=self.run, spawn=self.spawn,
                              terminate=self.terminate)


def test_a_successful_update_swaps_reopens_and_exits(runner, monkeypatch,
                                                     tmp_path):
    h = SwapHarness(monkeypatch, tmp_path, runner)
    result = h.update()

    assert result["ok"] and "0.2.0" in result["message"]
    # The old bundle went to the Trash, not to nowhere.
    trashed = list(h.trash.glob("*.app"))
    assert len(trashed) == 1 and "replaced" in trashed[0].name
    assert (trashed[0] / "Contents" / "old-marker").is_file()
    # The new one is where the old one was.
    assert (h.bundle / "Contents" / "Frameworks" / "build_info.json").is_file()
    # A detached reopener was spawned, aimed at the bundle path.
    assert any(str(h.bundle) in " ".join(cmd) for cmd in h.spawned)
    # The image was detached even though everything succeeded.
    assert any(cmd[:2] == ["hdiutil", "detach"] for cmd in h.commands)


def test_the_exit_happens_after_the_reply_not_instead_of_it(runner,
                                                            monkeypatch,
                                                            tmp_path):
    """The response must reach the page; the timer-then-exit is the contract.
    Here: perform_update returned (the reply exists) and the terminator was
    scheduled but not yet fired."""
    h = SwapHarness(monkeypatch, tmp_path, runner)
    result = h.update()
    assert result["ok"]
    assert h.terminated == []                # scheduled on a timer, not called
    import time
    time.sleep(2.0)
    assert h.terminated == [True]


def test_a_wrong_version_in_the_image_is_not_installed(runner, monkeypatch,
                                                       tmp_path):
    h = SwapHarness(monkeypatch, tmp_path, runner)
    h.stamp_version = "0.1.0"                # image says old, release says new
    with pytest.raises(UpdateError, match="not installing it"):
        h.update()
    # The installed app was never touched.
    assert (h.bundle / "Contents" / "old-marker").is_file()
    assert not list(h.trash.glob("*.app"))


def test_a_failed_install_puts_the_old_app_back(runner, monkeypatch, tmp_path):
    h = SwapHarness(monkeypatch, tmp_path, runner)
    inner = h.run

    def failing_final_cp(command, **kwargs):
        if command[0] == "cp" and command[3] == str(h.bundle):
            return subprocess.CompletedProcess(command, 1, "", "disk full")
        return inner(command, **kwargs)

    h.run = failing_final_cp
    with pytest.raises(UpdateError, match="Nothing was changed"):
        h.update()
    # Rolled back: the old bundle is home again, nothing in the Trash.
    assert (h.bundle / "Contents" / "old-marker").is_file()
    assert not list(h.trash.glob("*.app"))
    assert h.terminated == [] and h.spawned == []


# --- rebuilding from the checkout ---------------------------------------------
#
# The machine DocProof is written on has no release to install: the newest
# DocProof there is whatever is in the checkout. Nothing here runs git,
# pytest or PyInstaller — the subprocess boundary is the same injected `run`
# the swap uses.

class RebuildHarness:
    """A checkout with a virtualenv, and every command it would run, faked."""

    def __init__(self, monkeypatch, tmp_path, runner, *, fails=None,
                 version="0.3.0"):
        self.bundle = _frozen_at(monkeypatch, tmp_path)
        (self.bundle / "Contents" / "old-marker").write_text("old")
        self.trash = tmp_path / ".Trash"
        self.trash.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        self.source = tmp_path / "checkout"
        (self.source / ".git").mkdir(parents=True)
        (self.source / ".venv" / "bin").mkdir(parents=True)
        (self.source / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        monkeypatch.setattr(updatelib, "build_info",
                            lambda **kw: {"source": str(self.source),
                                          "version": version})

        self.fails = fails or {}
        self.commands: list[list[str]] = []
        self.stages: list[str] = []
        harness = self

        def run(command, **kwargs):
            harness.commands.append(command)
            what = harness._what(command)
            if what in harness.fails:
                code, err = harness.fails[what]
                return subprocess.CompletedProcess(command, code, "", err)
            if what == "remote":
                return subprocess.CompletedProcess(command, 0, "origin\n", "")
            if what == "build":
                app = (harness.source / "dist" / "DocProof.app" / "Contents"
                       / "Frameworks")
                app.mkdir(parents=True, exist_ok=True)
                (app / "build_info.json").write_text(
                    json.dumps({"version": version}))
            if command[0] == "cp":
                src, dst = Path(command[2]), Path(command[3])
                dst.mkdir(parents=True, exist_ok=True)
                for item in src.rglob("*"):
                    if item.is_file():
                        target = dst / item.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(item.read_bytes())
            return subprocess.CompletedProcess(command, 0, "", "")

        self.run = run
        self.spawned: list[list[str]] = []
        self.spawn = lambda cmd, **k: self.spawned.append(cmd)
        self.terminated = []
        self.terminate = lambda: self.terminated.append(True)
        self.runner = runner

    @staticmethod
    def _what(command) -> str:
        if command[0] == "git":
            return "remote" if command[-1] == "remote" else "pull"
        if "pytest" in command:
            return "tests"
        if "PyInstaller" in command:
            return "build"
        return command[0]

    def rebuild(self):
        return updatelib.rebuild_from_checkout(
            self.runner, run=self.run, spawn=self.spawn,
            terminate=self.terminate, progress=lambda s, m="": self.stages.append(s))


def test_a_rebuild_pulls_tests_builds_then_swaps(runner, monkeypatch, tmp_path):
    """The order tools/update.sh uses, for the reason it uses it: the tests run
    before anything is replaced."""
    h = RebuildHarness(monkeypatch, tmp_path, runner)

    version = h.rebuild()

    assert h.stages == ["pulling", "checking", "building", "installing"]
    assert version == "0.3.0"
    assert [h._what(c) for c in h.commands][:4] == ["remote", "pull", "tests",
                                                    "build"]


def test_a_rebuild_reopens_and_leaves_exiting_until_after_the_reply(
        runner, monkeypatch, tmp_path):
    """Same contract the release path has: the reopener is armed and the exit
    is on a timer, so whatever asked for this hears back before the process
    goes away."""
    h = RebuildHarness(monkeypatch, tmp_path, runner)

    h.rebuild()

    assert h.spawned and "open" in h.spawned[0][-1]
    assert h.terminated == []                # scheduled on a timer, not called


def test_the_old_app_goes_to_the_trash_not_nowhere(runner, monkeypatch,
                                                   tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)

    h.rebuild()

    kept = list(h.trash.glob("DocProof (replaced *).app"))
    assert kept and (kept[0] / "Contents" / "old-marker").exists()


def test_tests_that_fail_install_nothing(runner, monkeypatch, tmp_path):
    """The app you have is still the one that works."""
    h = RebuildHarness(monkeypatch, tmp_path, runner,
                       fails={"tests": (1, "3 failed, 500 passed")})

    with pytest.raises(UpdateError, match="tests do not pass") as refused:
        h.rebuild()

    assert "3 failed" in str(refused.value)   # pytest's own words, carried out
    assert (h.bundle / "Contents" / "old-marker").exists()
    assert not list(h.trash.glob("*.app"))
    assert "build" not in [h._what(c) for c in h.commands]


def test_a_build_that_fails_installs_nothing(runner, monkeypatch, tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner,
                       fails={"build": (1, "SyntaxError somewhere")})

    with pytest.raises(UpdateError, match="build failed"):
        h.rebuild()

    assert (h.bundle / "Contents" / "old-marker").exists()
    assert not h.spawned and not h.terminated


def test_a_pull_that_cannot_fast_forward_stops_before_anything_else(
        runner, monkeypatch, tmp_path):
    """A dirty tree or a diverged branch is a decision, and a decision needs a
    person."""
    h = RebuildHarness(monkeypatch, tmp_path, runner,
                       fails={"pull": (1, "error: Your local changes would be "
                                          "overwritten")})

    with pytest.raises(UpdateError, match="Nothing was changed"):
        h.rebuild()

    assert "tests" not in [h._what(c) for c in h.commands]


def test_a_checkout_with_no_remote_is_built_as_it_stands(runner, monkeypatch,
                                                         tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)
    h.fails = {}
    original = h.run

    def no_remote(command, **kwargs):
        if h._what(command) == "remote":
            return subprocess.CompletedProcess(command, 0, "", "")
        return original(command, **kwargs)

    h.run = no_remote
    h.rebuild()

    assert "pull" not in [h._what(c) for c in h.commands]


def test_a_build_whose_source_is_gone_says_so(runner, monkeypatch, tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)
    monkeypatch.setattr(updatelib, "build_info",
                        lambda **kw: {"source": str(tmp_path / "moved-away")})

    with pytest.raises(UpdateError, match="not a checkout"):
        h.rebuild()


def test_a_checkout_with_no_virtualenv_says_where_it_looked(runner, monkeypatch,
                                                            tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)
    (h.source / ".venv" / "bin" / "python").unlink()

    with pytest.raises(UpdateError, match="no virtualenv"):
        h.rebuild()


def test_a_rebuild_refuses_while_a_document_is_being_worked_on(runner,
                                                               monkeypatch,
                                                               tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)
    runner.store.save(Job(id="j-1", filename="a.docx", source_path="/x",
                          model="m", mode="now", state="running"))

    with pytest.raises(UpdateError, match="being worked on"):
        h.rebuild()

    assert h.commands == []


# --- the runner that holds one -----------------------------------------------

def test_the_rebuilder_reports_each_stage_then_says_it_is_done(runner,
                                                               monkeypatch,
                                                               tmp_path):
    h = RebuildHarness(monkeypatch, tmp_path, runner)
    seen = []
    monkeypatch.setattr(updatelib, "rebuild_from_checkout",
                        lambda r, progress=None, **kw: (
                            [progress(s) for s in ("pulling", "building")],
                            "0.3.0")[1])
    rebuilder = updatelib.Rebuilder()

    started = rebuilder.start(runner)
    assert started.state == "pulling"
    for _ in range(500):
        if rebuilder.state().state in ("done", "failed"):
            break
        import time as _t
        _t.sleep(0.01)

    assert rebuilder.state().state == "done"
    assert "0.3.0" in rebuilder.state().message


def test_a_rebuild_that_refuses_is_reported_not_raised(runner, monkeypatch,
                                                       tmp_path):
    def refuse(r, progress=None, **kw):
        raise UpdateError("A document is being worked on right now.")

    monkeypatch.setattr(updatelib, "rebuild_from_checkout", refuse)
    rebuilder = updatelib.Rebuilder()
    rebuilder.start(runner)
    for _ in range(500):
        if rebuilder.state().state in ("done", "failed"):
            break
        import time as _t
        _t.sleep(0.01)

    said = rebuilder.state()
    assert said.state == "failed" and "being worked on" in said.message


def test_only_one_rebuild_at_a_time(runner, monkeypatch):
    import threading
    gate, entered = threading.Event(), threading.Event()
    started = []

    def slow(r, progress=None, **kw):
        started.append(1)
        entered.set()
        gate.wait(5)
        return "0.3.0"

    monkeypatch.setattr(updatelib, "rebuild_from_checkout", slow)
    rebuilder = updatelib.Rebuilder()
    rebuilder.start(runner)
    entered.wait(5)

    rebuilder.start(runner)
    gate.set()

    assert len(started) == 1
