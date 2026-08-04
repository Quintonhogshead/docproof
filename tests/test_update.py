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
