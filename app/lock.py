"""One DocProof per app home.

Two copies sharing a job folder is not a cosmetic problem. Each has its own
worker thread, and `JobRunner.resume_interrupted` treats any job left in
`running` as one *it* abandoned in a crash — so the second copy adopts the
first copy's in-flight review, resets its progress, and runs it again from the
start. The document gets reviewed twice, the vendor bills for it twice, and
both write to the same results folder.

The desktop build already avoided this by leaving its port in a file and
attaching a second launch to the first. That only covers two launches of the
packaged app: a `docproof-app` run from a checkout, sharing the same default
home, walked straight past it.

So the guard belongs on the folder, not the port. `flock` is what makes it
reliable — the kernel drops the lock when the holding process dies, however it
dies, so there is no stale lock file to reason about and no PID to compare
against a table that recycles them.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:                           # pragma: no cover - Windows
    fcntl = None                              # see docs/windows.md

log = logging.getLogger("docproof.app.lock")

LOCK_NAME = "owner.lock"


class FolderInUse(RuntimeError):
    """Another DocProof already owns this app home.

    Carries whatever the holder wrote about itself, so the caller can name it
    rather than saying "something"."""

    def __init__(self, message: str, *, owner: dict | None = None):
        super().__init__(message)
        self.owner = owner or {}


class FolderLock:
    """An exclusive claim on one app home, held for as long as this process is.

    Advisory, like every flock — it stops DocProof racing DocProof, which is
    the only thing that has ever tried to."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / LOCK_NAME
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "FolderLock":
        """Claim the folder, or raise FolderInUse naming who has it."""
        if self._fd is not None:
            return self                       # already ours; acquiring is idempotent
        if fcntl is None:                     # pragma: no cover - Windows
            log.warning("No file locking on this platform; not checking "
                        "whether another DocProof owns %s", self.root)
            return self

        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
            # Reading is not blocked by an advisory lock, so the holder's own
            # description of itself is available to put in the message.
            owner = _read(fd)
            os.close(fd)
            raise FolderInUse(_in_use_message(self.root, owner),
                              owner=owner) from None

        # The file is only ever rewritten by whoever holds the lock, so this
        # cannot race another writer.
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(_describe(self.root), indent=2).encode("utf-8"))
        self._fd = fd
        log.info("This DocProof owns %s", self.root)
        return self

    def release(self) -> None:
        """Give the folder up. Safe to call twice, and on a lock never taken."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as e:                  # pragma: no cover - unmount, etc
            log.warning("Could not release %s: %s", self.path, e)
        finally:
            os.close(fd)
        # The file is deliberately left behind. Deleting it would race a copy
        # that has already claimed the folder, and a leftover is harmless:
        # the lock, not the file, is what says the folder is taken.

    def __enter__(self) -> "FolderLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def _describe(root: Path) -> dict:
    return {
        "pid": os.getpid(),
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Which copy to go and quit. A pid alone is precise and useless; what
        # the reader needs is whether to close a window or a terminal.
        "kind": "app" if getattr(sys, "frozen", False) else "terminal",
        "command": " ".join(sys.argv[:2])[:200],
        "root": str(root),
    }


def describe_owner(owner: dict) -> str:
    """The holder, in words, for whoever has to go and close it."""
    kinds = {"app": "The DocProof app is already open",
             "terminal": "A copy of DocProof started from a terminal is running"}
    who = kinds.get(owner.get("kind"), "Another copy of DocProof is running")
    pid = owner.get("pid")
    return f"{who} (process {pid})" if pid else who


def _read(fd: int) -> dict:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # A holder that claimed the lock but has not described itself yet.
        return {}


def _in_use_message(root: Path, owner: dict) -> str:
    return (
        f"{describe_owner(owner)}, using\n"
        f"  {root}\n\n"
        f"Two copies sharing one folder review the same documents twice, pay\n"
        f"for it twice, and write over each other's results.\n\n"
        f"Quit the other one first, or start this copy with --home pointing\n"
        f"somewhere else."
    )
