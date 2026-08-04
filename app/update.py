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
import subprocess
import sys
import tempfile
import threading
import time
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

    # The swap. Old bundle goes to the Trash rather than nowhere: if the new
    # build turns out broken, the previous one is a drag away, not gone.
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
        os.replace(trashed, bundle)          # put the old one back; still works
        raise UpdateError(f"Could not install the new DocProof ({e}). "
                          f"Nothing was changed.")

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


def _reopen_after_exit(bundle: Path, *, spawn=subprocess.Popen) -> None:
    """A detached script that waits for this process to die, then opens the
    new copy. It has to outlive us, which is the one thing we cannot do —
    hence Popen with its own session, never run(): run() would sit waiting
    for a shell that is itself waiting for us to exit."""
    script = (f'while kill -0 {os.getpid()} 2>/dev/null; '
              f'do sleep 0.3; done; open "{bundle}"')
    spawn(["/bin/sh", "-c", script], start_new_session=True)
