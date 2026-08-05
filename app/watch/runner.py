"""The watcher, held open inside the running app.

`tick` is a function a launchd process calls once and exits. The app is not
that process: it stays open for hours, somebody presses a button, and the pass
that follows can take an afternoon — five manuscripts, each a windowed pass
over a whole novel. So a pass gets a thread of its own, that thread takes the
folder lock, and everything the panel wants to know while it runs is read back
off the watch home's own job store, which the pass is already writing to.

**One pass at a time, ever**, guarded twice for two different reasons. The
folder lock is what stops the app racing a scheduled run started by launchd.
The single-flight flag is what stops the app racing *itself* — and it is not
redundant, because two `FolderLock`s over one path from one process are two
open file descriptions, and the kernel refuses the second exactly as if it
were another copy of DocProof. Without the flag, clicking a button twice would
put "quit the other DocProof" in front of somebody who had done nothing wrong.

**The lock is held only for the duration of a pass**, never by the app at
large. If the app claimed the watch home the way it claims its own, the
scheduled agent could never run while a window was open — and running when the
app is closed is the whole point of having one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.jobs import JobStore
from app.lock import FolderInUse, FolderLock, describe_owner
from app.settings import Paths

from . import auth as authlib
from . import tick as ticklib
from .drive import AuthExpired, DriveError
from .settings import WatchSettings
from .state import last_tick
from .status import missing

log = logging.getLogger("docproof.app.watch.runner")

# How often the in-app clock wakes to ask whether a pass is due. It only asks;
# `consider` decides, and mostly decides no.
CONSIDER_SECONDS = 60


@dataclass
class TickOutcome:
    """What one pass did, in a shape the panel can render."""

    started_at: str = ""
    finished_at: str = ""
    ok: bool = True
    listed: int = 0
    new: int = 0
    deferred: int = 0
    prepped: list[str] = field(default_factory=list)
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    # What went wrong, as a kind plus the library's own words. The kind is what
    # the route turns into prose: `tick.py` writes for somebody with a terminal
    # and an app user has none, so the translation happens at the front door
    # rather than by making the library ask who is calling.
    error_kind: str = ""      # "" | not_configured | auth_expired | drive
                              #    | in_use | other
    error: str = ""
    # The folder was already claimed, so this pass stood aside. Not a failure —
    # it is the arrangement working.
    skipped: bool = False


@dataclass
class SignIn:
    started_at: str = ""
    state: str = "waiting"    # waiting | done | failed
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WatchRunner:
    """Owns the watcher's threads, its lock, and what it last did."""

    def __init__(self, home: str | Path, *, agent_path: Path | None = None):
        self.home = Path(home)
        # Injected so a test can drive all of this without Google, launchctl,
        # or a real launch agent in ~/Library.
        self.agent_path = agent_path
        self.launchctl = None             # None → schedule.py's own default
        self._tick = ticklib.tick
        self._sign_in = authlib.sign_in

        self.started = False
        self._stop = threading.Event()
        self._mutex = threading.Lock()
        self._running = False
        self._started_at = ""
        self._last: TickOutcome | None = None
        self._signin: SignIn | None = None
        self._timer: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the clock. Starts no pass — opening DocProof should not spend
        money, and `consider` will not either unless it was asked to."""
        self.started = True
        self._timer = threading.Thread(target=self._clock,
                                       name="docproof-watch-clock", daemon=True)
        self._timer.start()

    def stop(self) -> None:
        """Ask everything to stop, and do not wait for a pass.

        A tick in the middle of a model call cannot be interrupted safely, and
        nothing is lost by leaving it: the checkpoint replays what was already
        paid for, and the Drive marker is written last, so a manuscript caught
        half-prepared is simply picked up by the next pass."""
        self._stop.set()

    @property
    def busy(self) -> bool:
        with self._mutex:
            return self._running

    # -- one pass -------------------------------------------------------------

    def run_now(self, *, mock: bool = False) -> bool:
        """Start a pass in the background. False if one is already going."""
        with self._mutex:
            if self._running:
                return False
            self._running = True
            self._started_at = _now()
        threading.Thread(target=self._pass, kwargs={"mock": mock},
                         name="docproof-watch-pass", daemon=True).start()
        return True

    def _pass(self, *, mock: bool) -> None:
        try:
            self.pass_once(mock=mock, _claimed=True)
        finally:
            with self._mutex:
                self._running = False

    def pass_once(self, *, mock: bool = False,
                  _claimed: bool = False) -> TickOutcome:
        """Look in the folder, once, on this thread.

        Public so a test can drive the body without a thread — the same reason
        `JobRunner.run_one` is."""
        if not _claimed:
            with self._mutex:
                self._running = True
                self._started_at = _now()
        out = TickOutcome(started_at=self._started_at or _now())
        ws = WatchSettings.load(self.home)
        try:
            with FolderLock(self.home):
                report = self._tick(self.home, ws, mock=mock)
        except FolderInUse as e:
            # Standing aside is the system working, not failing: a long book
            # can outlast the gap between two scheduled runs. The CLI makes the
            # same judgement and tells launchd nothing went wrong.
            out.skipped, out.error_kind = True, "in_use"
            out.error = describe_owner(e.owner)
            log.info("A pass stood aside: %s", out.error)
        except AuthExpired as e:
            out.ok, out.error_kind, out.error = False, "auth_expired", str(e)
        except ticklib.NotConfigured as e:
            out.ok, out.error_kind, out.error = False, "not_configured", str(e)
        except DriveError as e:
            out.ok, out.error_kind, out.error = False, "drive", str(e)
        except Exception as e:            # noqa: BLE001 - mirrors JobRunner._work
            log.exception("A watch pass failed")
            out.ok, out.error_kind, out.error = False, "other", str(e)
        else:
            out.listed, out.new = report.listed, report.new
            out.deferred = report.deferred
            out.prepped, out.uploaded = report.prepped, report.uploaded
            out.failed = report.failed
            out.ok = report.ok
        out.finished_at = _now()
        with self._mutex:
            self._last = out
            if not _claimed:
                self._running = False
        return out

    def preview(self) -> ticklib.TickReport:
        """What a pass would do. Synchronous, and claims nothing.

        A dry run reads the folder and stops — one token refresh and one
        listing, a second or two — so it needs no thread and no lock, and can
        answer even while a real pass is running."""
        return self._tick(self.home, WatchSettings.load(self.home),
                          dry_run=True)

    # -- signing in -----------------------------------------------------------

    def begin_sign_in(self, client_id: str, client_secret: str) -> SignIn:
        """Open Google's consent page and wait for the answer, off-thread.

        `run_flow` blocks for up to five minutes on a loopback server waiting
        for a browser. That cannot sit in a request: it would hold a worker for
        the whole conversation and the page would appear to hang."""
        with self._mutex:
            if self._signin and self._signin.state == "waiting":
                return self._signin
            self._signin = SignIn(started_at=_now())
        threading.Thread(
            target=self._sign_in_thread, args=(client_id, client_secret),
            name="docproof-watch-signin", daemon=True).start()
        return self._signin

    def _sign_in_thread(self, client_id: str, client_secret: str) -> None:
        try:
            self._sign_in(self.home, client_id, client_secret)
        except Exception as e:            # noqa: BLE001 - every refusal is prose
            log.warning("Signing in to Google did not finish: %s", e)
            done = SignIn(started_at=self._signin.started_at if self._signin
                          else _now(), state="failed", message=str(e))
        else:
            done = SignIn(started_at=self._signin.started_at if self._signin
                          else _now(), state="done",
                          message="Signed in. The sign-in is in your Keychain.")
        with self._mutex:
            self._signin = done

    def sign_in_state(self) -> SignIn | None:
        with self._mutex:
            return self._signin

    # -- the clock ------------------------------------------------------------

    def _clock(self) -> None:
        while not self._stop.wait(CONSIDER_SECONDS):
            try:
                self.consider()
            except Exception:             # noqa: BLE001 - mirrors JobRunner._tick
                log.exception("Deciding whether to look failed")

    def consider(self) -> bool:
        """Is a pass due? Public so a test can ask without a clock.

        Settings are re-read every time on purpose: turning automatic passes on
        or off in the panel takes effect at the next question rather than at
        the next restart."""
        ws = WatchSettings.load(self.home)
        if not ws.auto_ticks or missing(ws) is not None or self.busy:
            return False
        stamp = last_tick(self.home)
        if stamp is not None:
            due = stamp + timedelta(minutes=ws.tick_every_minutes)
            if datetime.now(timezone.utc) < due:
                # Something looked recently — quite possibly the scheduled
                # agent, a minute before the app was opened.
                return False
        return self.run_now()

    # -- what the panel reads -------------------------------------------------

    def state(self) -> dict:
        with self._mutex:
            last, running, since = self._last, self._running, self._started_at
        return {
            "busy": running,
            "started_at": since if running else "",
            "last": _outcome(last),
            "progress": self.progress(),
        }

    def progress(self) -> list[dict]:
        """What is being prepared right now.

        Free, and true: a pass writes `done`/`total` onto the job record as it
        goes, so this is the same number the pass itself is counting."""
        rows = []
        for job in self.jobs():
            if job.state in ("queued", "running", "collecting"):
                rows.append({"job_id": job.id, "filename": job.filename,
                             "plain_state": job.plain_state(),
                             "done": job.done, "total": job.total})
        return rows

    def jobs(self) -> list:
        """The watcher's own job records, or none at all.

        Never creates the store: a DocProof nobody has pointed at a folder
        should not grow a watch home because a tab was opened."""
        if not (self.home / "jobs").is_dir():
            return []
        try:
            return JobStore(Paths(self.home)).all()
        except OSError as e:              # noqa: BLE001 - reading is not the job
            log.warning("Could not read the watcher's jobs (%s)", e)
            return []

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Block until no pass is running. For tests and for shutdown."""
        deadline = time.monotonic() + timeout
        while self.busy:
            if time.monotonic() > deadline:
                raise TimeoutError("a watch pass did not settle")
            time.sleep(0.01)


def _outcome(out: TickOutcome | None) -> dict | None:
    if out is None:
        return None
    return {
        "started_at": out.started_at, "finished_at": out.finished_at,
        "ok": out.ok, "listed": out.listed, "new": out.new,
        "deferred": out.deferred, "prepped": list(out.prepped),
        "uploaded": list(out.uploaded),
        "failed": [{"name": n, "reason": r} for n, r in out.failed],
        "error_kind": out.error_kind, "error": out.error,
        "skipped": out.skipped,
    }
