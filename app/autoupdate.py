"""The Mac shell rebuilds itself when the code moves on, without being asked.

app/update.py already knows how to rebuild a packaged app from the checkout it
came from — but only when somebody presses a button ("Nothing here runs on
launch"). That is the right rule for a machine that might be mid-review; it is
the wrong one for Cover Canvas, which is opened, used for an afternoon, and
closed, while the code it was frozen from moves several times a day. The app
was silently a week old and nothing said so.

Three decisions make an automatic rebuild safe enough to run unattended:

- **It never touches the owner's checkout.** The build happens in a detached
  git WORKTREE of `origin/main` under the app home, refreshed with fetch and
  reset. The working tree the owner (and any agent) is using is only ever
  read: `git fetch` writes remote refs and nothing else. An automatic
  `git pull` in a tree somebody else is working in is exactly the clobber
  this avoids — and it also means the app follows what was actually MERGED,
  not whatever branch happens to be checked out.
- **It never interrupts.** A build is staged, not installed: the finished
  bundle is copied beside the app home and swapped in at the NEXT launch,
  before the window opens and before anything is loaded out of the bundle.
  Replacing a running app's bundle would pull its own data files out from
  under it (a PyInstaller onedir app reads config/, the static tree and its
  fonts at runtime), and killing a window to relaunch it is not something an
  update gets to do to somebody mid-cover.
- **It refuses to install anything that does not pass.** The suite runs in
  the worktree, with the checkout's own virtualenv, before a bundle is built
  — so a newer main that needs a dependency this venv does not have fails
  here, loudly, instead of being staged over the app that works.

Everything is best-effort and every failure is logged and swallowed: an app
that will not open because its updater had an opinion is worse than an app
that is a week old. `DOCPROOF_NO_AUTO_UPDATE=1` turns the whole thing off.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .settings import resource_root
from .update import (BUILD_TIMEOUT, PULL_TIMEOUT, TEST_TIMEOUT, UpdateError,
                     _swap, running_bundle)

log = logging.getLogger("docproof.app.autoupdate")

# Off switch, for a machine that would rather not spend the cycles — and for
# every test that must not shell out to git.
DISABLE_ENV = "DOCPROOF_NO_AUTO_UPDATE"

# Set on the child of a swap-and-relaunch, so a bundle that somehow still
# looks stale cannot send the app round the loop a second time.
RELAUNCHED_ENV = "DOCPROOF_UPDATE_RELAUNCHED"

# Everything this module owns lives under one directory in the app home: the
# build worktree, the staged bundle, and the note saying what is staged.
UPDATE_DIR = "updates"
SOURCE_DIR = "src"
STAGED_MARKER = "staged.json"

# Where the owner's real checkout is remembered. It has to be remembered
# because of what a successful update does to the evidence: the bundle this
# module builds is built INSIDE the update worktree, so its spec stamps that
# worktree as its `source` — and the worktree has no virtualenv. Trusting the
# stamp alone would make auto-update work exactly once and then quietly stop,
# reporting nothing worse than "no virtualenv to build with".
CHECKOUT_NOTE = "checkout.json"

# What to build from. Not the branch the checkout happens to be on: the app
# should follow what was merged, which is what everybody else is running too.
REMOTE = "origin"
BRANCH = "main"


@dataclass(frozen=True)
class Shell:
    """One packaged app, described the way its build describes itself.

    A dataclass rather than three module constants because there are two of
    these shells (DocProof and Cover Canvas), they are built by different
    specs into differently named bundles, and each stamps its build info
    under its own filename — so every one of those four strings has to travel
    together or a rebuild installs the wrong app over the right one."""

    name: str            # "Cover Canvas", for the log and the swap's messages
    spec: str            # the PyInstaller spec that builds it
    bundle: str          # what that spec produces in dist/
    build_info: str      # what the spec stamps into the bundle


CANVAS = Shell(name="Cover Canvas", spec="CoverCanvas.spec",
               bundle="Cover Canvas.app", build_info="canvas_build_info.json")
DOCPROOF = Shell(name="DocProof", spec="DocProof.spec",
                 bundle="DocProof.app", build_info="build_info.json")


def enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "") in ("", "0")


def stamp(shell: Shell) -> dict:
    """What this running build says about itself, or {} when it is not a
    build at all — running from a checkout, there is nothing to update."""
    path = resource_root() / shell.build_info
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _update_root(root: Path) -> Path:
    return Path(root) / UPDATE_DIR


def _marker_path(root: Path) -> Path:
    return _update_root(root) / STAGED_MARKER


def _staged(root: Path, shell: Shell) -> tuple[Path, dict] | None:
    """The bundle waiting to be installed, and what it is — or None.

    A marker whose bundle has gone (a cleared cache, a hand-emptied folder)
    is not an update; it is litter, and it answers None rather than sending
    the swap at a path that is not there."""
    try:
        note = json.loads(_marker_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    bundle = _update_root(root) / shell.bundle
    if not bundle.is_dir():
        return None
    return bundle, note


def _forget_staged(root: Path, shell: Shell) -> None:
    """Clear the staging area. Called after a swap, and after a staged build
    turns out to be one this app already is."""
    _marker_path(root).unlink(missing_ok=True)
    shutil.rmtree(_update_root(root) / shell.bundle, ignore_errors=True)


def install_staged(root: Path, shell: Shell, *, run=subprocess.run,
                   execv=os.execv) -> bool:
    """Put a staged build in place and hand over to it. Answers whether the
    app was replaced (in which case, normally, this call does not return).

    Called FIRST in a shell's main(), before the server, the window or a
    single data file out of the bundle: this is the one moment a bundle can
    be replaced safely, because nothing has been read out of it yet.

    Every refusal is silent and normal: no staged build, a staged build that
    is the one already running (an app rebuilt by hand in the meantime), or
    running from a checkout with no bundle to replace at all."""
    if not enabled() or os.environ.get(RELAUNCHED_ENV):
        return False
    bundle = running_bundle()
    if bundle is None:
        return False
    found = _staged(Path(root), shell)
    if found is None:
        return False
    fresh, note = found
    if note.get("commit") and note["commit"] == stamp(shell).get("commit"):
        _forget_staged(Path(root), shell)     # already what we are running
        return False

    try:
        _swap(bundle, fresh, run=run, name=shell.name)
    except UpdateError as e:
        log.warning("%s could not install the staged build: %s", shell.name, e)
        return False
    _forget_staged(Path(root), shell)
    log.info("%s updated to %s (%s); restarting into it.", shell.name,
             note.get("version", "a new build"), note.get("commit", "")[:9])
    os.environ[RELAUNCHED_ENV] = "1"
    try:
        execv(sys.executable, [sys.executable, *sys.argv[1:]])
    except OSError as e:
        # The new app IS installed — only the handover failed. Say so and
        # carry on with the code already loaded; the next launch is new.
        log.warning("%s installed the update but could not restart into it "
                    "(%s) — it will open next time.", shell.name, e)
    return True


def start(root: Path, shell: Shell, *, run=subprocess.run) -> threading.Thread | None:
    """Look for newer code and stage a build of it, in the background.

    Returns the thread (for tests) or None when there is nothing to do. A
    daemon thread: quitting the app mid-build stages nothing, which is the
    correct outcome — a half-built bundle is never installed because it is
    the MARKER, written last, that makes a build staged at all."""
    if not enabled():
        return None
    if running_bundle() is None:
        return None                    # running from source; nothing to build
    thread = threading.Thread(target=_work, args=(Path(root), shell),
                              kwargs={"run": run},
                              name="docproof-autoupdate", daemon=True)
    thread.start()
    return thread


def _work(root: Path, shell: Shell, *, run=subprocess.run) -> None:
    try:
        _stage_if_newer(root, shell, run=run)
    except Exception as e:                                  # noqa: BLE001
        # Never a traceback out of a background thread nobody asked for.
        log.info("%s could not check for an update: %s", shell.name, e)


def _git(args: list[str], cwd: Path, *, run, timeout: int = PULL_TIMEOUT
         ) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
               timeout=timeout)


def _out(done: subprocess.CompletedProcess) -> str:
    return (done.stdout or "").strip()


def _buildable(root: Path, candidate: Path) -> tuple[Path, Path] | None:
    """`candidate` and its virtualenv python, when it can build this app.

    A checkout with no .venv cannot build anything, and the update worktree
    is never an answer even when it looks like one: it is a detached copy of
    origin/main with no virtualenv, and treating it as the source would point
    every future update at itself."""
    if candidate == Path("") or not (candidate / ".git").exists():
        return None
    if candidate.resolve() == (_update_root(root) / SOURCE_DIR).resolve():
        return None
    python = candidate / ".venv" / "bin" / "python"
    return (candidate, python) if python.exists() else None


def _checkout(root: Path, info: dict) -> tuple[Path, Path] | None:
    """The checkout to build from, and its python.

    The build's own stamp first — it is the freshest thing there is — and the
    remembered one after it, for the launch after an auto-update, when the
    stamp points at the worktree that made it (see CHECKOUT_NOTE). Whatever
    answers is written down, so the next launch has it."""
    remembered = ""
    try:
        remembered = str(json.loads(
            (_update_root(root) / CHECKOUT_NOTE).read_text("utf-8")
        ).get("source") or "")
    except (OSError, ValueError):
        pass
    for candidate in (str(info.get("source") or ""), remembered):
        found = _buildable(root, Path(candidate)) if candidate else None
        if found is None:
            continue
        try:
            note = _update_root(root) / CHECKOUT_NOTE
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(json.dumps({"source": str(found[0])}, indent=2),
                            encoding="utf-8")
        except OSError:
            pass                              # remembering is a convenience
        return found
    return None


def _stage_if_newer(root: Path, shell: Shell, *, run) -> Path | None:
    """The whole job: fetch, compare, test, build, stage. Returns the staged
    bundle, or None when there was nothing newer (which is most launches)."""
    info = stamp(shell)
    built = str(info.get("commit") or "")
    found = _checkout(root, info)
    if found is None:
        log.debug("%s has no checkout with a virtualenv to build from "
                  "(built from %s).", shell.name, info.get("source") or
                  "somewhere")
        return None
    source, python = found

    # Read-only as far as the owner's working tree is concerned: fetch moves
    # remote refs and nothing else.
    fetched = _git(["fetch", "--quiet", REMOTE, BRANCH], source, run=run)
    if fetched.returncode != 0:
        log.debug("%s could not fetch %s/%s: %s", shell.name, REMOTE, BRANCH,
                  (fetched.stderr or "").strip())
        return None
    head = _out(_git(["rev-parse", "--short", f"{REMOTE}/{BRANCH}"], source,
                     run=run))
    if not head:
        return None
    if built and (head == built or head.startswith(built)
                  or built.startswith(head)):
        log.debug("%s is current with %s/%s (%s).", shell.name, REMOTE,
                  BRANCH, head)
        return None

    staged = _staged(root, shell)
    if staged and staged[1].get("commit", "").startswith(head):
        log.info("%s already has %s staged; it installs at the next launch.",
                 shell.name, head)
        return staged[0]

    work = _worktree(root, source, run=run)
    if work is None:
        return None
    log.info("%s is behind %s/%s (%s); building it.", shell.name, REMOTE,
             BRANCH, head)

    tests = run([str(python), "-m", "pytest", "-q"], cwd=str(work),
                capture_output=True, text=True, timeout=TEST_TIMEOUT)
    if tests.returncode != 0:
        tail = ((tests.stdout or tests.stderr or "").strip().splitlines()
                or ["pytest said nothing"])[-1]
        log.warning("%s did not stage %s: the tests do not pass (%s). The "
                    "app you have is still the one that works.",
                    shell.name, head, tail)
        return None

    build = run([str(python), "-m", "PyInstaller", "--noconfirm", shell.spec],
                cwd=str(work), capture_output=True, text=True,
                timeout=BUILD_TIMEOUT)
    fresh = work / "dist" / shell.bundle
    if build.returncode != 0 or not fresh.is_dir():
        tail = ((build.stderr or build.stdout or "").strip().splitlines()
                or ["PyInstaller said nothing"])[-1]
        log.warning("%s did not stage %s: the build failed (%s).",
                    shell.name, head, tail)
        return None
    return _stage(root, shell, fresh, head, run=run)


def _worktree(root: Path, source: Path, *, run) -> Path | None:
    """A detached checkout of `origin/main`, kept under the app home.

    A git worktree rather than a clone: it shares the checkout's object
    store, so it costs a working tree rather than a repository, and it can
    never be confused with the branch the owner is on — it is detached, by
    construction, at the commit that was fetched.

    Made once and reset thereafter. A worktree whose administrative record
    the repo has lost (a pruned .git/worktrees, a folder deleted by hand) is
    rebuilt from scratch rather than argued with."""
    work = _update_root(root) / SOURCE_DIR
    work.parent.mkdir(parents=True, exist_ok=True)
    target = f"{REMOTE}/{BRANCH}"
    if (work / ".git").exists():
        reset = _git(["reset", "--hard", "--quiet", target], work, run=run)
        if reset.returncode == 0:
            _git(["clean", "-qfdx", "-e", "dist", "-e", "build"], work,
                 run=run)
            return work
        log.debug("Rebuilding the update worktree at %s: %s", work,
                  (reset.stderr or "").strip())
        shutil.rmtree(work, ignore_errors=True)
        _git(["worktree", "prune"], source, run=run)

    added = _git(["worktree", "add", "--detach", "--force", str(work), target],
                 source, run=run)
    if added.returncode != 0:
        log.debug("Could not make an update worktree at %s: %s", work,
                  (added.stderr or "").strip())
        return None
    return work


def _stage(root: Path, shell: Shell, fresh: Path, commit: str, *,
           run) -> Path | None:
    """Copy the finished bundle into the staging area and write the marker
    that makes it count.

    The marker is written LAST and is the only thing install_staged trusts:
    a copy interrupted halfway leaves a bundle nobody will ever install,
    rather than a broken app somebody will."""
    area = _update_root(root)
    area.mkdir(parents=True, exist_ok=True)
    destination = area / shell.bundle
    _marker_path(root).unlink(missing_ok=True)
    shutil.rmtree(destination, ignore_errors=True)
    done = run(["cp", "-R", str(fresh), str(destination)],
               capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if done.returncode != 0:
        log.warning("%s built %s but could not stage it: %s", shell.name,
                    commit, (done.stderr or "").strip())
        shutil.rmtree(destination, ignore_errors=True)
        return None
    version = ""
    try:
        version = str(json.loads(
            (destination / "Contents" / "Frameworks" / shell.build_info)
            .read_text("utf-8")).get("version", ""))
    except (OSError, ValueError):
        pass
    _marker_path(root).write_text(json.dumps({
        "commit": commit,
        "version": version,
        "bundle": shell.bundle,
        "staged": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    log.info("%s %s is staged; it installs the next time this app opens.",
             shell.name, version or commit)
    return destination


__all__ = ["BRANCH", "CANVAS", "DISABLE_ENV", "DOCPROOF", "REMOTE",
           "RELAUNCHED_ENV", "Shell", "enabled", "install_staged", "stamp",
           "start"]
