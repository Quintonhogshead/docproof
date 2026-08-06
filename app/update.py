"""Installing a newer DocProof over this one, at the user's request.

The pieces already exist: the app knows which release is newest and can fetch
its disk image (app/version.py). What this adds is the last step — put the new
bundle where this one is and reopen — done the only way an app can replace
itself: stage the new copy, move the old bundle aside, move the new one in,
then hand a tiny detached script the job of reopening it once this process has
exited. The folder lock (app/lock.py) lifts with the process, so the new copy
starts clean.

Three refusals matter more than the mechanism:

- never while a job is running — a review mid-flight would be killed (it
  would *resume* from its checkpoint now, but "your review restarted because
  the app updated" is still not a thing an update should do);
- never when running from the source — there is nothing to replace;
- never from a disk image or a translocated path — macOS runs unsigned apps
  from a DMG in a randomized read-only mirror, and replacing *that* is
  replacing a shadow. The fix is dragging DocProof to Applications, so the
  message says so.

One click is still required. Nothing here runs on launch; the launch-time
check only *asks* GitHub and shows a banner.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .version import build_info, check_for_update, download_release

log = logging.getLogger("docproof.app.update")

MOUNT_TIMEOUT = 120


class UpdateError(RuntimeError):
    """The update did not happen; the message is written for the user."""


def running_bundle() -> Path | None:
    """The .app this process lives in, or None when running from a checkout."""
    if not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).parents:
        if parent.suffix == ".app":
            return parent
    return None


def _is_translocated(bundle: Path) -> bool:
    text = str(bundle)
    return "/AppTranslocation/" in text or text.startswith("/Volumes/")


def refuse_reason(runner) -> str | None:
    """Why an update cannot happen right now, or None. Checked before any
    download, because the polite time to refuse is before work, not after."""
    bundle = running_bundle()
    if bundle is None:
        return ("You are running DocProof from its source, so there is "
                "nothing to replace — pull and rebuild instead.")
    if _is_translocated(bundle):
        return ("DocProof is running straight from its disk image. Drag it "
                "to Applications first, open it from there, then update.")
    busy = runner.queue.qsize() > 0 or runner._busy.is_set() or any(
        j.state in ("running", "collecting") for j in runner.store.all())
    if busy:
        return ("A document is being worked on right now. Updating would "
                "interrupt it — let it finish, then update.")
    return None


def perform_update(runner, *, run=subprocess.run, spawn=subprocess.Popen,
                   terminate=None) -> dict:
    """Download the newest release, swap this bundle for it, and reopen.

    Returns the success payload the HTTP route sends back; the actual exit
    happens a moment later so the reply reaches the page first. Everything
    that can go wrong before the swap leaves the installed app untouched."""
    reason = refuse_reason(runner)
    if reason:
        raise UpdateError(reason)
    bundle = running_bundle()

    checked = check_for_update()
    if not checked.get("ok"):
        raise UpdateError(checked["message"])
    if checked.get("current"):
        raise UpdateError(checked["message"])

    staging = Path(tempfile.mkdtemp(prefix="docproof-update-"))
    got = download_release(into=staging)
    if not got.get("ok"):
        raise UpdateError(got["message"])
    dmg = Path(got["path"])

    fresh = _extract_app(dmg, staging, run=run)
    _check_it_is_newer(fresh, checked)
    _swap(bundle, fresh, run=run)

    _reopen_after_exit(bundle, spawn=spawn)
    version = checked.get("tag", "").lstrip("v") or "the new version"
    log.info("Updated to %s; restarting.", version)

    # Exit *after* the HTTP response has gone out. A daemon thread with a
    # short fuse is crude and exactly sufficient; tests inject a no-op.
    if terminate is None:
        terminate = lambda: os._exit(0)      # noqa: E731 - the whole point
    threading.Timer(1.5, terminate).start()
    return {"ok": True,
            "message": f"DocProof {version} is installed — reopening now."}


def _swap(bundle: Path, fresh: Path, *, run) -> None:
    """Put `fresh` where `bundle` is, keeping the old one recoverable.

    The old bundle goes to the Trash rather than nowhere: if the new build
    turns out broken, the previous one is a drag away, not gone. And if the
    copy fails, the old one goes straight back — "nothing was changed" is a
    promise the rollback keeps."""
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    trashed = (Path.home() / ".Trash" /
               f"{bundle.stem} (replaced {stamp}).app")
    try:
        os.replace(bundle, trashed)
    except OSError as e:
        raise UpdateError(f"Could not move the old DocProof aside: {e}")
    try:
        # Not os.replace: the staged copy may be on another volume, and cp -R
        # preserves the bundle's structure and permissions exactly.
        done = run(["cp", "-R", str(fresh), str(bundle)],
                   capture_output=True, text=True)
        if done.returncode != 0:
            raise OSError(done.stderr.strip() or "cp failed")
    except OSError as e:
        # A cp that died partway left a partial bundle at the destination,
        # and rename(2) will not replace a non-empty directory — so the
        # debris has to go before the old app can take its place back.
        shutil.rmtree(bundle, ignore_errors=True)
        try:
            os.replace(trashed, bundle)      # put the old one back; still works
        except OSError as undo:
            raise UpdateError(
                f"Could not install the new DocProof ({e}), and putting the "
                f"old one back failed too ({undo}). The old app is in the "
                f"Trash as “{trashed.name}” — drag it back to "
                f"{bundle.parent}.")
        raise UpdateError(f"Could not install the new DocProof ({e}). "
                          f"Nothing was changed.")


def _extract_app(dmg: Path, staging: Path, *, run) -> Path:
    """The .app out of the disk image, copied so the image can detach."""
    mount = staging / "mount"
    mount.mkdir()
    attached = run(["hdiutil", "attach", str(dmg), "-nobrowse", "-readonly",
                    "-mountpoint", str(mount)],
                   capture_output=True, text=True, timeout=MOUNT_TIMEOUT)
    if attached.returncode != 0:
        raise UpdateError("The downloaded disk image would not open. "
                          "Try again — it may not have downloaded fully.")
    try:
        apps = sorted(mount.glob("*.app"))
        if not apps:
            raise UpdateError("The disk image has no application in it.")
        fresh = staging / apps[0].name
        copied = run(["cp", "-R", str(apps[0]), str(fresh)],
                     capture_output=True, text=True)
        if copied.returncode != 0:
            raise UpdateError("Could not copy the new DocProof out of its "
                              "disk image.")
        return fresh
    finally:
        run(["hdiutil", "detach", str(mount), "-quiet"],
            capture_output=True, text=True)


def _check_it_is_newer(fresh: Path, checked: dict) -> None:
    """The staged bundle must actually be the release we were promised —
    a truncated download that still mounts should fail here, not in the
    user's Dock tomorrow."""
    stamp = fresh / "Contents" / "Frameworks" / "build_info.json"
    try:
        import json
        version = json.loads(stamp.read_text("utf-8")).get("version", "")
    except (OSError, ValueError):
        raise UpdateError("The downloaded DocProof does not say which version "
                          "it is; not installing it.")
    wanted = (checked.get("tag") or "").lstrip("vV")
    if wanted and version != wanted:
        raise UpdateError(f"The downloaded DocProof says it is {version}, "
                          f"but the release is {wanted}; not installing it.")


# --- rebuilding from the checkout this build came from ------------------------
#
# Everything above installs a *published* DocProof: fetch a release, swap it in.
# That is right for a Mac somebody was sent an .app on, and useless on the one
# the app is written on, where the newest DocProof is not a release at all — it
# is whatever is in the checkout. That machine used to be told to go and run
# `tools/update.sh` in a terminal, which is a strange thing for an application
# to say when it knows perfectly well where its own source is.
#
# So this is that script, behind the same button: pull, run the tests, build,
# and swap in the result. It takes about a minute, which is too long to hold an
# HTTP request open, so it runs on a thread and the page asks how it is going.

BUILD_TIMEOUT = 900
TEST_TIMEOUT = 900
PULL_TIMEOUT = 120

# What the page shows while it waits. The vocabulary is deliberately about the
# app rather than the toolchain: nobody pressed a button to run PyInstaller.
STAGE_TEXT = {
    "pulling": "Fetching the latest changes…",
    "checking": "Checking everything still works…",
    "building": "Building DocProof — this is the slow part…",
    "installing": "Installing it…",
}


@dataclass
class Rebuild:
    state: str = "idle"       # idle | pulling | checking | building
                              #      | installing | done | failed
    message: str = ""
    started_at: str = ""

    @property
    def running(self) -> bool:
        return self.state in STAGE_TEXT


class Rebuilder:
    """Runs one rebuild, and answers how it is going.

    A thread rather than a request, because a build is a minute and a request
    that long looks like a hang. One at a time, and never a second after a
    successful one — the process is about to exit and be replaced."""

    def __init__(self):
        self._mutex = threading.Lock()
        self._state = Rebuild()

    def state(self) -> Rebuild:
        with self._mutex:
            return Rebuild(**vars(self._state))

    def start(self, runner, **kw) -> Rebuild:
        with self._mutex:
            if self._state.running:
                return Rebuild(**vars(self._state))
            self._state = Rebuild(
                state="pulling", message=STAGE_TEXT["pulling"],
                started_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"))
            # The snapshot is taken here rather than re-read after the thread
            # starts: a rebuild that fails in its first moment would otherwise
            # be reported as the answer to "did it start?", which it is not.
            begun = Rebuild(**vars(self._state))
        threading.Thread(target=self._run, args=(runner,), kwargs=kw,
                         name="docproof-rebuild", daemon=True).start()
        return begun

    def _say(self, state: str, message: str = "") -> None:
        with self._mutex:
            self._state.state = state
            self._state.message = message or STAGE_TEXT.get(state, "")

    def _run(self, runner, **kw) -> None:
        try:
            version = rebuild_from_checkout(runner, progress=self._say, **kw)
        except UpdateError as e:
            log.warning("Rebuild refused: %s", e)
            self._say("failed", str(e))
        except Exception as e:            # noqa: BLE001 - never lose the reason
            log.exception("Rebuild failed")
            self._say("failed", str(e))
        else:
            self._say("done", f"DocProof {version} is installed — reopening "
                              f"now.")


def rebuild_from_checkout(runner, *, run=subprocess.run,
                          spawn=subprocess.Popen, terminate=None,
                          progress=None, skip_tests: bool = False) -> str:
    """Rebuild this app from the checkout it was built from, and install it.

    The same steps `tools/update.sh --install` takes, in the same order and for
    the same reasons — the tests run before anything is replaced, because
    installing a build that does not work over somebody's only copy is the
    failure worth preventing."""
    say = progress or (lambda *a, **k: None)
    reason = refuse_reason(runner)
    if reason:
        raise UpdateError(reason)
    bundle = running_bundle()

    source = Path(build_info(runner=run).get("source") or "")
    if not (source / ".git").exists():
        raise UpdateError(
            f"This build says it came from {source or 'somewhere'}, which is "
            f"not a checkout any more. There is nothing here to rebuild from.")
    python = source / ".venv" / "bin" / "python"
    if not python.exists():
        raise UpdateError(
            f"There is no virtualenv at {python}, so DocProof cannot build "
            f"itself. Create one in {source} and try again.")

    say("pulling")
    _pull(source, run=run)

    if not skip_tests:
        say("checking")
        _run_tests(source, python, run=run)

    say("building")
    fresh = _build(source, python, run=run)

    say("installing")
    _swap(bundle, fresh, run=run)
    _reopen_after_exit(bundle, spawn=spawn)

    version = _stamped_version(bundle) or "the new version"
    log.info("Rebuilt from %s as %s; restarting.", source, version)
    if terminate is None:
        terminate = lambda: os._exit(0)      # noqa: E731 - the whole point
    threading.Timer(1.5, terminate).start()
    return version


def _pull(source: Path, *, run) -> None:
    """Fast-forward only, and only when there is somewhere to pull from.

    Anything cleverer than a fast-forward is a decision, and a decision needs a
    person — so a dirty tree or a diverged branch is reported rather than
    resolved."""
    remotes = run(["git", "-C", str(source), "remote"], capture_output=True,
                  text=True, timeout=PULL_TIMEOUT)
    if not (remotes.stdout or "").strip():
        return                               # a local-only checkout is fine
    done = run(["git", "-C", str(source), "pull", "--ff-only"],
               capture_output=True, text=True, timeout=PULL_TIMEOUT)
    if done.returncode != 0:
        raise UpdateError(
            "Could not fetch the latest changes: "
            + ((done.stderr or done.stdout or "").strip().splitlines() or
               ["git would not say why"])[-1]
            + ". Nothing was changed.")


def _run_tests(source: Path, python: Path, *, run) -> None:
    done = run([str(python), "-m", "pytest", "-q"], cwd=str(source),
               capture_output=True, text=True, timeout=TEST_TIMEOUT)
    if done.returncode != 0:
        tail = (done.stdout or done.stderr or "").strip().splitlines()
        raise UpdateError(
            "The tests do not pass, so nothing was installed — the app you "
            "have is still the one that works. "
            + (tail[-1] if tail else "pytest said nothing."))


def _build(source: Path, python: Path, *, run) -> Path:
    done = run([str(python), "-m", "PyInstaller", "--noconfirm",
                "DocProof.spec"], cwd=str(source), capture_output=True,
               text=True, timeout=BUILD_TIMEOUT)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise UpdateError("The build failed, so nothing was installed. "
                          + (tail[-1] if tail else "PyInstaller said nothing."))
    fresh = source / "dist" / "DocProof.app"
    if not fresh.is_dir():
        raise UpdateError("The build finished but produced no DocProof.app.")
    return fresh


def _stamped_version(bundle: Path) -> str:
    try:
        import json
        stamp = bundle / "Contents" / "Frameworks" / "build_info.json"
        return str(json.loads(stamp.read_text("utf-8")).get("version", ""))
    except (OSError, ValueError):
        return ""


def _reopen_after_exit(bundle: Path, *, spawn=subprocess.Popen) -> None:
    """A detached script that waits for this process to die, then opens the
    new copy. It has to outlive us, which is the one thing we cannot do —
    hence Popen with its own session, never run(): run() would sit waiting
    for a shell that is itself waiting for us to exit."""
    script = (f'while kill -0 {os.getpid()} 2>/dev/null; '
              f'do sleep 0.3; done; open "{bundle}"')
    spawn(["/bin/sh", "-c", script], start_new_session=True)
