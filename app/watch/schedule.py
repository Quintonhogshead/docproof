"""Handing the watcher to launchd, so nobody has to remember it.

Twice a day is a `StartCalendarInterval` with two entries, and the rest
of this file is the small print around that: which executable, where its
output goes, and the honest caveat that a Mac asleep at midnight
does not run anything at midnight.

`RunAtLoad` is false on purpose. Installing a schedule should not immediately
spend money — the first run happens at the first scheduled time, and anybody
who wants one now can type `docproof-watch once`.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("docproof.app.watch.schedule")

LABEL = "com.atmospherepress.docproof.watch"
DEFAULT_TIMES = ("00:00", "12:00")
LAUNCHD_LOG = "launchd.log"
# What a packaged DocProof answers to when it should do one pass and stop.
# Spelled the same here and in docproof_desktop.py, which reads it.
WATCH_ONCE = "--watch-once"

# launchd starts a job with almost no environment. LibreOffice is found by
# absolute path first, so this is belt: a Homebrew install answers to `which`
# and nothing else here would find it.
PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class ScheduleError(RuntimeError):
    """A schedule that could not be set, said so a person can act on."""


def parse_times(spec: str | list[str]) -> list[tuple[int, int]]:
    """"06:00,11:00" → [(6, 0), (11, 0)]."""
    parts = spec.split(",") if isinstance(spec, str) else list(spec)
    times = []
    for part in parts:
        piece = part.strip()
        if not piece:
            continue
        hour, _, minute = piece.partition(":")
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            raise ScheduleError(f"{piece!r} is not a time. Write them as "
                                f"HH:MM, separated by commas — for example "
                                f"{','.join(DEFAULT_TIMES)}.") from None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ScheduleError(f"There is no such time as {piece}.")
        times.append((h, m))
    if not times:
        raise ScheduleError("No times were given. Write them as HH:MM, "
                            f"separated by commas — for example "
                            f"{','.join(DEFAULT_TIMES)}.")
    return sorted(set(times))


def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable() -> str:
    """The program launchd should run, by absolute path.

    A packaged DocProof has no `docproof-watch` anywhere on the machine —
    somebody who was sent an .app has the app and nothing else — so the bundle
    schedules its own binary, which knows how to do one pass and stop. From a
    checkout, `sys.argv[0]` first, because the copy running right now is the
    copy that should be scheduled: a machine with two virtualenvs would
    otherwise get whichever one is earlier in a PATH launchd does not share."""
    if frozen():
        exe = Path(sys.executable).resolve()
        if "/AppTranslocation/" in str(exe) or str(exe).startswith("/Volumes/"):
            # macOS runs an app off a disk image from a randomised read-only
            # mirror. A schedule pointing there works until the image is
            # ejected and then fails silently every morning, which is the worst
            # way for this to break.
            raise ScheduleError(
                "DocProof is running straight from its disk image, so the path "
                "macOS would remember disappears when you eject it. Drag "
                "DocProof to your Applications folder, open it from there, and "
                "try again.")
        return str(exe)
    argv0 = Path(sys.argv[0])
    if argv0.name == "docproof-watch" and argv0.exists():
        return str(argv0.resolve())
    found = shutil.which("docproof-watch")
    if found:
        return str(Path(found).resolve())
    raise ScheduleError(
        "Could not find the `docproof-watch` command to schedule. Install "
        "DocProof with `pip install -e .` in its folder and try again.")


def program(home: Path) -> list[str]:
    """The home is spelled out rather than left to the environment: launchd
    passes almost none of one, and a scheduled run that quietly used a
    different folder would be a puzzle nobody enjoys."""
    last = WATCH_ONCE if frozen() else "once"
    return [executable(), "--home", str(Path(home).resolve()), last]


def plist_content(times, *, command: list[str], log_path: Path) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command,
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in times],
        "RunAtLoad": False,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PATH": PATH},
        "ProcessType": "Background",
    })



def install(times, home: str | Path, *, run=subprocess.run,
            path: Path | None = None) -> Path:
    """Write the agent and load it, replacing any earlier one."""
    root = Path(home)
    target = path or plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plist_content(times, command=program(root),
                                     log_path=root / LAUNCHD_LOG))

    domain = f"gui/{os.getuid()}"
    # Unloaded first so a changed schedule replaces the old one rather than
    # being refused for already existing. It fails when nothing is loaded,
    # which is the ordinary case the first time and not worth reporting.
    run(["launchctl", "bootout", f"{domain}/{LABEL}"],
        capture_output=True, text=True)
    result = run(["launchctl", "bootstrap", domain, str(target)],
                 capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise ScheduleError(
            f"macOS would not start the schedule{': ' + detail if detail else '.'}"
            f" The agent is written at {target}; `launchctl bootstrap "
            f"{domain} {target}` is what failed.")
    return target


def uninstall(*, run=subprocess.run, path: Path | None = None) -> bool:
    """Stop and forget the schedule. Answers whether there was one."""
    target = path or plist_path()
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"],
        capture_output=True, text=True)
    if not target.exists():
        return False
    target.unlink()
    return True


def current(*, path: Path | None = None) -> list[tuple[int, int]] | None:
    """The times an installed agent runs at, or None if there is none.

    Read from the file rather than asked of launchctl: the question `status`
    is answering is what this Mac has been told to do, which the file is the
    record of."""
    target = path or plist_path()
    if not target.is_file():
        return None
    try:
        data = plistlib.loads(target.read_bytes())
    except Exception as e:                # noqa: BLE001 - any unreadable plist
        log.warning("Ignoring an unreadable launch agent at %s (%s)", target, e)
        return None
    entries = data.get("StartCalendarInterval") or []
    return [(int(e.get("Hour", 0)), int(e.get("Minute", 0)))
            for e in entries if isinstance(e, dict)]


def describe(times) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
