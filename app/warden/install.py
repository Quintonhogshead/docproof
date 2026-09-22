"""launchd plists for the Warden's two clocks.

Same pattern as `galley/agent.py`'s own installer (`_install_launchd`,
`plist_content`): a `plistlib`-built definition under `~/Library/LaunchAgents`,
`launchctl bootout` before `bootstrap` so re-installing is idempotent, and a
log file under the Warden's own home rather than stdout going nowhere. Two
labels because the plan calls for two independently-scheduled clocks — a
full `tick` every `tick_interval_min` and a cheap `listen` every
`listen_interval_min` — not because they need separate installers; the two
plists differ only in label, interval, and which subcommand they run.

`agents_dir` is a parameter, not a hardcoded path, purely so a test can point
this at a `tmp_path` instead of the real `~/Library/LaunchAgents` and check
the plists it writes without touching the machine's actual launchd."""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("docproof.app.warden.install")

TICK_LABEL = "com.docproof.warden-tick"
LISTEN_LABEL = "com.docproof.warden-listen"


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _plist_bytes(*, label: str, subcommand: str, interval_s: int,
                 log_path: Path, workdir: Path) -> bytes:
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "app.warden.cli", subcommand],
        "StartInterval": interval_s,
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "WorkingDirectory": str(workdir),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                 "HOME": str(Path.home())},
        "ProcessType": "Background",
    })


def install(*, home: str | Path, interval_min: int = 20,
           listen_interval_min: int = 2, run=subprocess.run,
           agents_dir: Path | None = None) -> tuple[Path, Path]:
    """Write both plists and (re)load them. Returns `(tick_plist, listen_plist)`."""
    target_dir = agents_dir or default_agents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(home) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    workdir = _repo_root()

    tick_plist = target_dir / f"{TICK_LABEL}.plist"
    tick_plist.write_bytes(_plist_bytes(
        label=TICK_LABEL, subcommand="tick",
        interval_s=max(60, int(interval_min) * 60),
        log_path=logs_dir / "tick.log", workdir=workdir))

    listen_plist = target_dir / f"{LISTEN_LABEL}.plist"
    listen_plist.write_bytes(_plist_bytes(
        label=LISTEN_LABEL, subcommand="listen",
        interval_s=max(30, int(listen_interval_min) * 60),
        log_path=logs_dir / "listen.log", workdir=workdir))

    domain = f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}"
    for label, path in ((TICK_LABEL, tick_plist), (LISTEN_LABEL, listen_plist)):
        # A missing prior service is normal on first install; bootout is
        # best-effort and its failure is never reported.
        run(["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True, text=True)
        result = run(["launchctl", "bootstrap", domain, str(path)],
                     capture_output=True, text=True)
        if getattr(result, "returncode", 0) != 0:
            detail = (getattr(result, "stderr", "") or "").strip()
            log.warning("launchctl would not start %s%s", label,
                       f": {detail}" if detail else "")

    return tick_plist, listen_plist


def uninstall(*, run=subprocess.run, agents_dir: Path | None = None) -> list[Path]:
    """Stop and remove both plists. Returns the paths actually removed."""
    target_dir = agents_dir or default_agents_dir()
    domain = f"gui/{os.getuid() if hasattr(os, 'getuid') else 0}"
    removed: list[Path] = []
    for label in (TICK_LABEL, LISTEN_LABEL):
        run(["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True, text=True)
        path = target_dir / f"{label}.plist"
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def installed(*, agents_dir: Path | None = None) -> bool:
    target_dir = agents_dir or default_agents_dir()
    return (target_dir / f"{TICK_LABEL}.plist").is_file()


__all__ = ["TICK_LABEL", "LISTEN_LABEL", "default_agents_dir", "install",
          "uninstall", "installed"]
