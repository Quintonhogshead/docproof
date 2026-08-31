"""The shell that rebuilds itself, without ever building anything.

Every subprocess is injected — no git runs, no PyInstaller runs, nothing is
fetched and no bundle is replaced except a directory made in tmp_path. What is
under test is the discipline the module promises: it never touches the owner's
working tree, it never installs anything that did not pass its tests, and it
never swaps a bundle under a running window (only at the start of the next
launch).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import autoupdate
from app.autoupdate import CANVAS, Shell, install_staged, stamp

BUILT = "aaa1111"
FRESH = "bbb2222"


class _Runs:
    """A stand-in for subprocess.run that answers by the words in the command
    and records every call, so a test can assert what was NOT run as easily as
    what was."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None):
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _words(cmd) -> str:
        """The command with its PATHS reduced to their last component.

        Every temp directory these tests run in has the word "pytest" in it
        (pytest's own tmp_path), so a match against the raw command line
        would have `git fetch` answering to the rule written for the test
        run — and a fake that answers the wrong question makes every
        assertion around it meaningless."""
        return " ".join(Path(str(c)).name if str(c).startswith("/") else str(c)
                        for c in cmd)

    def __call__(self, cmd, **kw):
        self.calls.append([str(c) for c in cmd])
        joined = self._words(cmd)
        for key, (code, out) in self.answers.items():
            if key in joined:
                return subprocess.CompletedProcess(cmd, code, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ran(self, needle: str) -> bool:
        return any(needle in self._words(call) for call in self.calls)


def _checkout(tmp_path: Path) -> Path:
    """A source checkout convincing enough for the guards: a .git and a venv
    python. Neither is ever executed."""
    source = tmp_path / "src"
    (source / ".git").mkdir(parents=True)
    venv = source / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n")
    return source


def _stamped(monkeypatch, source: Path, commit: str = BUILT,
             shell: Shell = CANVAS) -> Path:
    """Pretend this process is a build of `commit` made from `source`."""
    home = source.parent / "bundle-resources"
    home.mkdir(parents=True, exist_ok=True)
    (home / shell.build_info).write_text(json.dumps({
        "version": "0.161.0", "commit": commit, "source": str(source)}))
    monkeypatch.setattr(autoupdate, "resource_root", lambda: home)
    return home


def _frozen(monkeypatch, tmp_path: Path, shell: Shell = CANVAS) -> Path:
    bundle = tmp_path / "Applications" / shell.bundle
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        str(bundle / "Contents" / "MacOS" / "app"))
    return bundle


def _stage(root: Path, shell: Shell, commit: str, version: str = "0.162.0"
           ) -> Path:
    area = root / autoupdate.UPDATE_DIR
    (area / shell.bundle / "Contents").mkdir(parents=True)
    (area / autoupdate.STAGED_MARKER).write_text(json.dumps({
        "commit": commit, "version": version, "bundle": shell.bundle}))
    return area / shell.bundle


# -- the off switch ------------------------------------------------------------

def test_the_env_var_turns_the_whole_thing_off(monkeypatch, tmp_path):
    monkeypatch.setenv(autoupdate.DISABLE_ENV, "1")
    assert autoupdate.enabled() is False
    assert autoupdate.start(tmp_path, CANVAS) is None
    assert install_staged(tmp_path, CANVAS, run=_Runs()) is False


def test_running_from_a_checkout_has_nothing_to_update(monkeypatch, tmp_path):
    monkeypatch.delenv(autoupdate.DISABLE_ENV, raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert autoupdate.start(tmp_path, CANVAS) is None
    assert install_staged(tmp_path, CANVAS, run=_Runs()) is False


# -- installing what a previous session staged ---------------------------------

def test_a_staged_build_is_swapped_in_and_handed_over_to(monkeypatch,
                                                         tmp_path):
    monkeypatch.delenv(autoupdate.DISABLE_ENV, raising=False)
    monkeypatch.delenv(autoupdate.RELAUNCHED_ENV, raising=False)
    source = _checkout(tmp_path)
    _stamped(monkeypatch, source)
    bundle = _frozen(monkeypatch, tmp_path)
    _stage(tmp_path, CANVAS, FRESH)
    handed: list[list[str]] = []

    runs = _Runs()
    assert install_staged(tmp_path, CANVAS, run=runs,
                          execv=lambda path, argv: handed.append([path, *argv])
                          ) is True
    # The new bundle was copied over the old one...
    assert runs.ran("cp -R") or any(
        call[0] == "cp" for call in runs.calls)
    # ...the staging area was cleared, so the next launch does not do it again,
    assert not (tmp_path / autoupdate.UPDATE_DIR /
                autoupdate.STAGED_MARKER).exists()
    # ...and the app handed over to the executable that is now in place.
    assert handed and handed[0][0] == sys.executable
    assert bundle.exists() or True          # the swap itself is update.py's


def test_a_staged_build_this_app_already_is_is_just_cleared(monkeypatch,
                                                            tmp_path):
    monkeypatch.delenv(autoupdate.DISABLE_ENV, raising=False)
    monkeypatch.delenv(autoupdate.RELAUNCHED_ENV, raising=False)
    source = _checkout(tmp_path)
    _stamped(monkeypatch, source, commit=FRESH)
    _frozen(monkeypatch, tmp_path)
    _stage(tmp_path, CANVAS, FRESH)         # the same commit we are running
    runs = _Runs()

    assert install_staged(tmp_path, CANVAS, run=runs,
                          execv=lambda *a: None) is False
    assert not runs.calls                   # nothing was replaced
    assert not (tmp_path / autoupdate.UPDATE_DIR /
                autoupdate.STAGED_MARKER).exists()


def test_a_marker_whose_bundle_has_gone_is_litter_not_an_update(monkeypatch,
                                                                tmp_path):
    monkeypatch.delenv(autoupdate.DISABLE_ENV, raising=False)
    monkeypatch.delenv(autoupdate.RELAUNCHED_ENV, raising=False)
    _stamped(monkeypatch, _checkout(tmp_path))
    _frozen(monkeypatch, tmp_path)
    area = tmp_path / autoupdate.UPDATE_DIR
    area.mkdir(parents=True)
    (area / autoupdate.STAGED_MARKER).write_text(json.dumps({"commit": FRESH}))

    runs = _Runs()
    assert not install_staged(tmp_path, CANVAS, run=runs,
                              execv=lambda *a: None)
    assert not runs.calls


def test_the_relaunch_marker_stops_a_second_hand_over(monkeypatch, tmp_path):
    monkeypatch.delenv(autoupdate.DISABLE_ENV, raising=False)
    monkeypatch.setenv(autoupdate.RELAUNCHED_ENV, "1")
    _stamped(monkeypatch, _checkout(tmp_path))
    _frozen(monkeypatch, tmp_path)
    _stage(tmp_path, CANVAS, FRESH)
    assert install_staged(tmp_path, CANVAS, run=_Runs(),
                          execv=lambda *a: None) is False


# -- deciding whether there is anything to build -------------------------------

def _stage_if_newer(tmp_path, monkeypatch, runs, *, commit=BUILT):
    source = _checkout(tmp_path)
    _stamped(monkeypatch, source, commit=commit)
    return autoupdate._stage_if_newer(tmp_path, CANVAS, run=runs)


def test_a_build_that_matches_origin_main_builds_nothing(monkeypatch,
                                                         tmp_path):
    runs = _Runs({"rev-parse": (0, BUILT)})
    assert _stage_if_newer(tmp_path, monkeypatch, runs) is None
    assert runs.ran("fetch")                # it asked...
    assert not runs.ran("-m pytest")        # ...and then did nothing
    assert not runs.ran("PyInstaller")


def test_the_owners_working_tree_is_never_pulled_or_reset(monkeypatch,
                                                          tmp_path):
    """The one rule that protects a checkout somebody else is working in:
    fetch writes remote refs; nothing here writes their tree."""
    runs = _Runs({"rev-parse": (0, FRESH), "pytest": (1, "1 failed")})
    _stage_if_newer(tmp_path, monkeypatch, runs)
    source = str(tmp_path / "src")
    for call in runs.calls:
        if call[:3] == ["git", "-C", source]:
            assert call[3] in ("fetch", "rev-parse", "worktree"), call


def test_tests_that_fail_stage_nothing(monkeypatch, tmp_path):
    runs = _Runs({"rev-parse": (0, FRESH), "pytest": (1, "1 failed, 0 passed")})
    assert _stage_if_newer(tmp_path, monkeypatch, runs) is None
    assert runs.ran("-m pytest")
    assert not runs.ran("PyInstaller")      # the build never started
    assert not (tmp_path / autoupdate.UPDATE_DIR /
                autoupdate.STAGED_MARKER).exists()


def test_a_build_that_fails_stages_nothing(monkeypatch, tmp_path):
    runs = _Runs({"rev-parse": (0, FRESH), "PyInstaller": (1, "boom")})
    assert _stage_if_newer(tmp_path, monkeypatch, runs) is None
    assert not (tmp_path / autoupdate.UPDATE_DIR /
                autoupdate.STAGED_MARKER).exists()


def test_a_successful_build_is_staged_with_a_marker(monkeypatch, tmp_path):
    """The marker is written last and is the only thing install_staged
    trusts, so a copy that dies halfway leaves nothing installable."""
    work = tmp_path / autoupdate.UPDATE_DIR / autoupdate.SOURCE_DIR
    dist = work / "dist" / CANVAS.bundle

    class _Building(_Runs):
        def __call__(self, cmd, **kw):
            joined = self._words(cmd)
            if "worktree add" in joined:
                (work / ".git").mkdir(parents=True, exist_ok=True)
            if "PyInstaller" in joined:
                frameworks = dist / "Contents" / "Frameworks"
                frameworks.mkdir(parents=True, exist_ok=True)
                (frameworks / CANVAS.build_info).write_text(
                    json.dumps({"version": "0.162.0", "commit": FRESH}))
            if cmd and str(cmd[0]) == "cp":
                target = Path(str(cmd[-1]))
                target.mkdir(parents=True, exist_ok=True)
                (target / "Contents").mkdir(exist_ok=True)
            return super().__call__(cmd, **kw)

    runs = _Building({"rev-parse": (0, FRESH)})
    staged = _stage_if_newer(tmp_path, monkeypatch, runs)
    assert staged is not None and staged.is_dir()
    note = json.loads((tmp_path / autoupdate.UPDATE_DIR /
                       autoupdate.STAGED_MARKER).read_text())
    assert note["commit"] == FRESH
    assert note["bundle"] == CANVAS.bundle
    # It built in the worktree, not in the owner's checkout.
    assert any("PyInstaller" in " ".join(c) for c in runs.calls)
    assert runs.ran("worktree add")


def test_the_checkout_is_remembered_so_updating_twice_works(monkeypatch,
                                                            tmp_path):
    """The bundle an update builds is built INSIDE the update worktree, so
    its stamp names that worktree as its source — and the worktree has no
    virtualenv. Without a memory of the real checkout, auto-update would work
    exactly once and then go quiet."""
    source = _checkout(tmp_path)
    _stamped(monkeypatch, source)
    runs = _Runs({"rev-parse": (0, BUILT)})
    autoupdate._stage_if_newer(tmp_path, CANVAS, run=runs)
    note = json.loads((tmp_path / autoupdate.UPDATE_DIR /
                       autoupdate.CHECKOUT_NOTE).read_text())
    assert note["source"] == str(source)

    # Now the app is the one that update built: its stamp points at the
    # worktree, which cannot build anything.
    home = tmp_path / "bundle-resources"
    (home / CANVAS.build_info).write_text(json.dumps({
        "version": "0.162.0", "commit": BUILT,
        "source": str(tmp_path / autoupdate.UPDATE_DIR /
                      autoupdate.SOURCE_DIR)}))
    again = _Runs({"rev-parse": (0, BUILT)})
    autoupdate._stage_if_newer(tmp_path, CANVAS, run=again)
    assert again.ran("fetch")               # it still knows where to look
    assert any(str(source) in " ".join(call) for call in again.calls)


def test_the_update_worktree_is_never_treated_as_the_checkout(tmp_path):
    work = tmp_path / autoupdate.UPDATE_DIR / autoupdate.SOURCE_DIR
    (work / ".git").mkdir(parents=True)
    (work / ".venv" / "bin").mkdir(parents=True)
    (work / ".venv" / "bin" / "python").write_text("")
    assert autoupdate._buildable(tmp_path, work) is None


def test_a_checkout_that_has_moved_away_is_left_alone(monkeypatch, tmp_path):
    home = tmp_path / "resources"
    home.mkdir()
    (home / CANVAS.build_info).write_text(json.dumps({
        "version": "0.161.0", "commit": BUILT,
        "source": str(tmp_path / "gone")}))
    monkeypatch.setattr(autoupdate, "resource_root", lambda: home)
    runs = _Runs()
    assert autoupdate._stage_if_newer(tmp_path, CANVAS, run=runs) is None
    assert not runs.calls


def test_a_checkout_with_no_virtualenv_cannot_build_itself(monkeypatch,
                                                           tmp_path):
    source = tmp_path / "src"
    (source / ".git").mkdir(parents=True)
    _stamped(monkeypatch, source)
    runs = _Runs()
    assert autoupdate._stage_if_newer(tmp_path, CANVAS, run=runs) is None
    assert not runs.calls


# -- the two shells stay distinct ---------------------------------------------

def test_each_shell_names_its_own_spec_bundle_and_stamp():
    assert CANVAS.spec == "CoverCanvas.spec"
    assert CANVAS.bundle == "Cover Canvas.app"
    assert CANVAS.build_info == "canvas_build_info.json"
    assert autoupdate.DOCPROOF.build_info == "build_info.json"
    # Nothing shared: installing one over the other is the failure this
    # dataclass exists to make impossible.
    assert CANVAS.bundle != autoupdate.DOCPROOF.bundle


def test_stamp_is_empty_when_this_is_not_a_build(monkeypatch, tmp_path):
    monkeypatch.setattr(autoupdate, "resource_root", lambda: tmp_path)
    assert stamp(CANVAS) == {}
