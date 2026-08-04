"""What this build is, and whether the source it came from has moved on.

DocProof is built on the machine it runs on, from a checkout the user owns.
There is no update server to ask and — the bundle being unsigned — no honest
way to download and run new code on somebody's behalf. So "is there a newer
version?" is answered locally: the build remembers which commit it was made
from and where that repository is, and checking means asking git whether it
has moved since. Updating means rebuilding, which `tools/update.sh` does.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from docproof import __version__

from .settings import resource_root

log = logging.getLogger("docproof.app.version")

BUILD_INFO = "build_info.json"


def _git(args: list[str], cwd: Path, *, runner=subprocess.run) -> str:
    """One git answer, or "" — every caller here has a sensible thing to say
    when git is missing, the directory is not a checkout, or the commit the
    build was made from is no longer in the history."""
    try:
        done = runner(["git", "-C", str(cwd), *args], capture_output=True,
                      text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("git %s failed: %s", " ".join(args), e)
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def build_info(*, runner=subprocess.run) -> dict:
    """This build, described.

    A packaged .app reads what the spec stamped into it. Run from a checkout
    there is nothing stamped and nothing to stamp — the source *is* what is
    running — so the same questions are put to git directly."""
    stamped = resource_root() / BUILD_INFO
    if stamped.is_file():
        try:
            info = json.loads(stamped.read_text("utf-8"))
            info["frozen"] = True
            return info
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable %s (%s)", BUILD_INFO, e)

    root = resource_root()
    return {
        "version": __version__,
        "built": "",                     # nothing was built; this is the source
        "commit": _git(["rev-parse", "--short", "HEAD"], root, runner=runner),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], root,
                       runner=runner),
        "source": str(root),
        "frozen": False,
    }


def check_for_update(*, runner=subprocess.run) -> dict:
    """Has the source this build came from moved on?

    Returns something the Settings screen can print as-is. Every failure is a
    sentence rather than an error: the repository having been moved, renamed or
    never cloned is a normal thing to discover, not a fault."""
    info = build_info(runner=runner)

    if not info["frozen"]:
        dirty = _git(["status", "--porcelain"], Path(info["source"]),
                     runner=runner)
        return {"ok": True, "current": True, "info": info,
                "message": "You are running from the source, so this is as new "
                           "as it gets."
                           + (" It has uncommitted changes in it."
                              if dirty else "")}

    source = Path(info.get("source") or "")
    if not source.is_dir():
        return {"ok": False, "info": info,
                "message": f"This build came from {source or 'somewhere'}, "
                           f"which is not on this Mac any more. Rebuild from "
                           f"wherever the DocProof source lives now."}

    head = _git(["rev-parse", "--short", "HEAD"], source, runner=runner)
    if not head:
        return {"ok": False, "info": info,
                "message": f"{source} is not a git checkout any more, so there "
                           f"is nothing to compare this build against."}

    built_from = info.get("commit") or ""
    if head == built_from:
        return {"ok": True, "current": True, "info": info, "head": head,
                "message": f"Up to date — this is the latest build of "
                           f"{info['version']}."}

    behind = _git(["rev-list", "--count", f"{built_from}..HEAD"], source,
                  runner=runner) if built_from else ""
    if behind.isdigit() and int(behind) > 0:
        n = int(behind)
        what = f"{n} change{'' if n == 1 else 's'}"
    else:
        # The commit it was built from is not an ancestor of HEAD: a rebase, a
        # different branch, or a build from a checkout since rewritten.
        what = "changes"
    return {"ok": True, "current": False, "info": info, "head": head,
            "behind": behind,
            "message": f"There {'is' if what.startswith('1 ') else 'are'} "
                       f"{what} in {source} that this build does not have. "
                       f"Run tools/update.sh there to rebuild."}
