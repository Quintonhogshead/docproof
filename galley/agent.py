"""Poll DocWatch for manuscripts, run the Galley driver, and upload results.

Track claims and pending deliveries in a local ledger. Service installers
support launchd on macOS and systemd on Linux.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from docproof import agent_lane
from docproof.subscription_limits import UsageLimitError, is_usage_limited, resume_after
from galley.driver import CredentialsError

log = logging.getLogger("docproof.galley.agent")

# Credentials file; group/other permissions are forbidden. The sifter side
# reads the same path (docproof.agent_lane) when Claude Code has not passed
# the token down to it.
DEFAULT_ENV_FILE = agent_lane.DEFAULT_CREDENTIALS_FILE
#: The ledger of what this machine has claimed, finished and failed.
LEDGER_NAME = ".agent-state.json"
USAGE_PAUSE_NAME = ".subscription-pause.json"
#: Where the service writes everything the agent says, on either platform.
LOG_NAME = "agent.log"
# launchd service label.
LABEL = "com.atmosphere.galley-agent"
# Store downloads by Drive id to separate identically named books.
DOWNLOAD_DIR = ".agent-downloads"

DEFAULT_POLL_INTERVAL_S = 300.0
#: What the server calls the read-only route this poller lives on.
AWAITING_PATH = "/api/watch/awaiting"
#: Where the agent reports what it is doing, so the Proofread drawer can show
#: it — the second and last route a machine may touch, write-only.
STATUS_PATH = "/api/watch/agent"
#: Optional: who gets the agent's own alerts (boot, a poll that stopped
#: working, a delivery given up on). Falls back to the watcher's notify
#: address.
ALERT_EMAIL_KEY = "GALLEY_ALERT_EMAIL"
ALERT_TAGS = "[DocProof][Galley][Agent]"
#: While a book runs, how often the drawer hears from the agent even when no
#: phase boundary passes. 0 disables the timer (tests).
DEFAULT_HEARTBEAT_S = 60.0
#: How long the sign-in check may take before it counts as a failure.
PREFLIGHT_TIMEOUT_S = 180.0
#: What to do when the subscription token is rejected. One place, quoted by
#: the alert, the heartbeat and the log.
TOKEN_FIX_HINT = (
    "Make a new token with `claude setup-token` on a Mac that is signed in, "
    "then: on Fly, `fly secrets set -a atmosphere-docproof "
    "GALLEY_OAUTH_TOKEN=<token>` (the agent machine restarts and resumes the "
    "claimed book); on a Mac, replace CLAUDE_CODE_OAUTH_TOKEN in "
    "~/.galley/agent.env (picked up at the next poll).")
# Service PATH defaults include the CLI and common Homebrew locations.
PATH = ("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
        + str(Path.home() / ".local" / "bin"))

# Named credential keys; additional file values also pass into the driver
# environment.
OAUTH_KEY = agent_lane.OAUTH_TOKEN_KEY
APP_URL_KEY = "GALLEY_APP_URL"
AGENT_TOKEN_KEY = "GALLEY_AGENT_TOKEN"


class AgentError(RuntimeError):
    """Invalid agent configuration."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



@dataclass(frozen=True)
class AgentEnv:
    """Agent credentials and additional environment values passed to the
    driver.
    """

    app_url: str
    token: str
    oauth_token: str
    values: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @property
    def awaiting_url(self) -> str:
        return self.app_url.rstrip("/") + AWAITING_PATH

    @property
    def status_url(self) -> str:
        return self.app_url.rstrip("/") + STATUS_PATH

    @property
    def alert_email(self) -> str:
        return (self.values.get(ALERT_EMAIL_KEY) or "").strip()


#: One implementation of the file format, in docproof.agent_lane — the sifter
#: children read the same file for the same token and cannot import galley.
parse_env = agent_lane.parse_env


def read_env(path: str | Path = DEFAULT_ENV_FILE, *,
             stat_fn: Callable[[Path], Any] | None = None) -> AgentEnv:
    """Load credentials; reject group/other permissions and missing required
    values.
    """
    target = Path(str(path)).expanduser()
    try:
        values = agent_lane.read_credentials(target, stat_fn=stat_fn)
    except agent_lane.CredentialsError as e:
        raise AgentError(str(e)) from e
    if values is None:
        raise AgentError(
            f"No agent credentials at {target}. Create it with:\n"
            f"    mkdir -p {target.parent} && touch {target} && "
            f"chmod 600 {target}\n"
            f"then put {OAUTH_KEY} (from `claude setup-token`), "
            f"{APP_URL_KEY} and {AGENT_TOKEN_KEY} in it.")
    missing = [k for k in (OAUTH_KEY, APP_URL_KEY, AGENT_TOKEN_KEY)
               if not values.get(k)]
    if missing:
        raise AgentError(
            f"{target} is missing {', '.join(missing)}. {OAUTH_KEY} comes from "
            f"`claude setup-token`; {APP_URL_KEY} is the DocProof app's "
            f"address (e.g. https://atmosphere-docproof.fly.dev); "
            f"{AGENT_TOKEN_KEY} is the same secret as the server's "
            f"DOCPROOF_AGENT_TOKEN.")
    return AgentEnv(app_url=values[APP_URL_KEY], token=values[AGENT_TOKEN_KEY],
                    oauth_token=values[OAUTH_KEY], values=values, path=target)


def check_credentials(values: dict[str, str], *, runner=subprocess.run,
                      timeout_s: float = PREFLIGHT_TIMEOUT_S) -> str:
    """Sign in once with the subscription token, cheaply, and return "" when
    Claude Code answers — or the reason it did not.

    One turn of a one-line prompt on the subscription: the cost of finding
    out before a book is claimed, rather than after its profile phase has
    been written off as needs_human.
    """
    from galley.driver import STRIPPED_KEYS, detect_credential_failure

    env = dict(os.environ)
    env.update({k: v for k, v in values.items() if v})
    for key in STRIPPED_KEYS:             # the session signs in on the token
        env.pop(key, None)
    if not env.get(OAUTH_KEY):
        return f"{OAUTH_KEY} is not set"
    argv = ["claude", "-p", "Reply with exactly the word: ok",
            "--max-turns", "1", "--output-format", "json"]
    try:
        proc = runner(argv, env=env, capture_output=True, text=True,
                      timeout=timeout_s)
    except FileNotFoundError:
        return "the `claude` command is not installed on this machine"
    except subprocess.TimeoutExpired:
        return f"`claude -p` did not answer within {timeout_s:.0f}s"
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    try:
        reply = json.loads(proc.stdout or "{}")
        detail = str(reply.get("result") or output) if isinstance(reply, dict) else output
    except ValueError:
        detail = output
    if is_usage_limited(detail):
        return f"Claude subscription usage limit: {detail[:800]}"
    if detect_credential_failure(output):
        line = next((ln.strip() for ln in output.splitlines()
                     if detect_credential_failure(ln)), "")
        return f"Claude Code refused the subscription token: {line[:300]}"
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-5:])
        return (f"`claude -p` exited {proc.returncode} before the token "
                f"could be confirmed: {tail[:400]}")
    return ""


def apply_env(env: AgentEnv, *, environ: dict[str, str] | None = None) -> None:
    """Copy credential-file values into the process environment, overriding
    existing values. This also supplies Google credentials on hosts without
    a keyring backend.
    """
    target = environ if environ is not None else os.environ
    for key, value in env.values.items():
        if value:
            target[key] = value



@dataclass(frozen=True)
class AwaitingBook:
    """One book DocWatch is waiting on a practitioner for."""

    file_id: str
    name: str
    folder_id: str = ""
    author_last: str = ""
    request_id: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "AwaitingBook":
        return cls(file_id=str(raw.get("file_id", "")),
                   name=str(raw.get("name", "")),
                   folder_id=str(raw.get("folder_id")
                                 or raw.get("subfolder_id") or ""),
                   author_last=str(raw.get("author_last", "")),
                   request_id=str(raw.get("request_id", "")))


def _open_url(request: urllib.request.Request, timeout: int = 30):
    """The one place this module touches the network, passed in by every caller
    so no test ever reaches Fly."""
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_awaiting(env: AgentEnv, *, opener=_open_url) -> list[AwaitingBook]:
    """Fetch awaiting manuscripts; log request failures and return an empty
    list.
    """
    return poll_awaiting(env, opener=opener)[0]


def poll_awaiting(env: AgentEnv, *, opener=_open_url
                  ) -> tuple[list[AwaitingBook], str]:
    """The awaiting list and, when the poll failed, one line saying how — so
    the caller can tell "nothing to do" from "could not ask"."""
    request = urllib.request.Request(
        env.awaiting_url,
        headers={"Authorization": f"Bearer {env.token}",
                 "Accept": "application/json"})
    try:
        with opener(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:                                   # noqa: BLE001
            pass
        error = (f"the app refused the awaiting list (HTTP {e.code})"
                 f"{': ' + detail if detail else ''}")
        log.warning("The app refused the awaiting list (HTTP %s)%s",
                    e.code, f": {detail}" if detail else "")
        return [], error
    except Exception as e:                                  # noqa: BLE001
        log.warning("Could not reach %s (%s); trying again next poll.",
                    env.awaiting_url, e)
        return [], f"could not reach {env.awaiting_url} ({e})"
    if not isinstance(payload, dict):
        log.warning("The app answered something that is not an awaiting list.")
        return [], "the app answered something that is not an awaiting list"
    books = [AwaitingBook.from_json(row)
             for row in (payload.get("books") or []) if isinstance(row, dict)]
    return [b for b in books if b.file_id and b.name], ""


def post_status(env: AgentEnv, payload: dict[str, Any], *,
                opener=_open_url) -> bool:
    """Tell the app what the agent is doing. Never raises: a heartbeat that
    cannot land must not stop the work it reports on."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        env.status_url, data=body, method="POST",
        headers={"Authorization": f"Bearer {env.token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with opener(request, 15) as response:
            response.read()
        return True
    except Exception as e:                                  # noqa: BLE001
        log.warning("Heartbeat did not land at %s (%s).", env.status_url, e)
        return False


def send_alert(env: AgentEnv, subject: str, body: str, *,
               to: str = "", get_key=None, opener=None) -> bool:
    """Email the agent's own alert over the watcher's Gmail sign-in — the
    same token Drive uploads use. Quiet no-op without an address or a
    sign-in; never raises."""
    try:
        from app.watch import notify
        from app.watch.settings import WatchSettings, default_watch_home
        from galley.driver import drive_token

        address = to or env.alert_email
        if not address:
            ws = WatchSettings.load(default_watch_home())
            address = (ws.notify_email or "").strip()
        if not address:
            log.info("No alert address (%s); not emailing: %s",
                     ALERT_EMAIL_KEY, subject)
            return False
        token = drive_token(get_key=get_key)
        kwargs = {"opener": opener} if opener is not None else {}
        notify.send(token, address, f"{ALERT_TAGS} {subject}", body, **kwargs)
        return True
    except Exception as e:                                  # noqa: BLE001
        log.warning("Agent alert could not be emailed (%s): %s", e, subject)
        return False



CLAIMED, FINISHED, FAILED = "claimed", "finished", "failed"
#: A claimed book's operational_status while it waits for a new version.
HELD_FOR_CODE = "held_for_code"


def code_id() -> str:
    """What "a new version" means to a held book: the deployed image on Fly
    (every deploy changes it, bumped version or not), else the package
    version."""
    from docproof import __version__
    image = os.environ.get("FLY_IMAGE_REF", "").strip()
    return f"{__version__}@{image}" if image else __version__


# Retry incomplete delivery without rerunning the book, up to
# MAX_DELIVERY_ATTEMPTS.
PENDING_DELIVERY = "pending_delivery"
MAX_DELIVERY_ATTEMPTS = 6
#: Backoff between delivery retries, in poll intervals: 1, 2, 4, 8, ...
DELIVERY_BACKOFF_BASE = 2


@dataclass
class Ledger:
    """Persist claims, outcomes, and delivery progress by Drive file id."""

    path: Path
    books: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        target = Path(path)
        ledger = cls(target)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ledger
        if isinstance(raw, dict) and isinstance(raw.get("books"), dict):
            ledger.books = {str(k): dict(v) for k, v in raw["books"].items()
                            if isinstance(v, dict)}
        return ledger

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"schema_version": 1, "books": self.books}, indent=2,
                       ensure_ascii=False), encoding="utf-8")

    def state(self, file_id: str) -> str:
        return str(self.books.get(file_id, {}).get("state", ""))

    def record(self, file_id: str, state: str, **fields: Any) -> None:
        entry = self.books.setdefault(file_id, {})
        entry.update(fields)
        entry["state"] = state
        entry["updated_at"] = _now()
        entry.setdefault("claimed_at", entry["updated_at"])
        self.save()

    def claimed(self, file_id: str) -> dict[str, Any]:
        return dict(self.books.get(file_id, {}))

    def pending(self) -> list[str]:
        """Books claimed but never finished — a crash mid-run leaves these."""
        return sorted(k for k, v in self.books.items()
                      if v.get("state") == CLAIMED)

    def pending_deliveries(self) -> list[str]:
        """Books whose verdict is written but not yet uploaded."""
        return sorted(k for k, v in self.books.items()
                      if v.get("state") == PENDING_DELIVERY)



_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug_for(name: str, author_last: str = "", file_id: str = "") -> str:
    """Use the surname and Drive id suffix as the workspace name. Without an
    id, use the filename stem, then the surname or a placeholder.
    """
    if file_id:
        from galley.driver import workspace_slug
        return workspace_slug(name, author_last, file_id)
    stem = Path(name).stem
    slug = _SLUG_STRIP.sub("-", stem.lower()).strip("-")
    if slug:
        return slug
    fallback = _SLUG_STRIP.sub("-", (author_last or "").lower()).strip("-")
    return f"{fallback}-book" if fallback else "untitled-book"



@dataclass
class RunReport:
    """What one poll did, for the log and for `--once`'s exit code."""

    looked_at: int = 0
    claimed: str = ""
    outcome: str = ""
    reason: str = ""
    skipped: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    #: Why no book was (or will be) claimed: the subscription token is
    #: rejected. The claimed book, if any, stays claimed and resumes later.
    halted: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"looked_at": self.looked_at, "claimed": self.claimed,
                "outcome": self.outcome, "reason": self.reason,
                "skipped": list(self.skipped), "halted": self.halted}


@dataclass
class Agent:
    """Poll for manuscripts with injectable network, driver, upload, and clock
    functions.
    """

    env: AgentEnv
    workspace_root: Path = Path("~/galley-workspaces")
    budget_usd: float | None = None
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    drive_folder_override: str = ""
    #: Injected seams: the app, Drive, the driver, and the clock.
    opener: Callable = _open_url
    download: Callable[[AwaitingBook, Path], Path] | None = None
    run_driver: Callable[..., Any] | None = None
    upload: Callable[[list[Path], str], list[str]] | None = None
    verify_upload: Callable[[Path, str, str], bool] | None = None
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = log.info
    #: Observability seams. `heartbeat` receives the whole status dict each
    #: time it changes (default: POST it to the app); `alert` receives
    #: (subject, body) for the agent's own alarms (default: email).
    heartbeat: Callable[[dict[str, Any]], None] | None = None
    alert: Callable[[str, str], None] | None = None
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_S
    host: str = field(default_factory=socket.gethostname)
    #: The sign-in check (`check_credentials`), run at boot and again before
    #: claiming while the token is known to be bad. None skips it — the
    #: driver still recognises a rejected token mid-run.
    preflight: Callable[[dict[str, str]], str] | None = None
    wall_clock: Callable[[], float] = time.time
    _status: dict[str, Any] = field(default_factory=dict, repr=False)
    _poll_error: str = field(default="", repr=False)
    #: The current credentials failure, or "" while the token works.
    _halt: str = field(default="", repr=False)
    _file_id: str = field(default="", repr=False)


    @property
    def root(self) -> Path:
        return Path(str(self.workspace_root)).expanduser()

    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def ledger(self) -> Ledger:
        return Ledger.load(self.ledger_path)

    # --- what the drawer sees ------------------------------------------------

    def _beat(self, **changes: Any) -> dict[str, Any]:
        """Merge `changes` into the agent's status and send it. Never raises."""
        from docproof import __version__

        self._status.update({k: v for k, v in changes.items()})
        payload = {"agent": self.host, "version": __version__, "at": _now(),
                   "poll_interval_s": self.poll_interval_s,
                   "app": self.env.awaiting_url, **self._status}
        try:
            if self.heartbeat is not None:
                self.heartbeat(payload)
            else:
                post_status(self.env, payload, opener=self.opener)
        except Exception:                                   # noqa: BLE001
            log.warning("heartbeat failed", exc_info=True)
        return payload

    def _alarm(self, subject: str, body: str) -> None:
        """One of the agent's own alerts. Never raises."""
        self.log(f"ALERT {subject}")
        try:
            if self.alert is not None:
                self.alert(subject, body)
            else:
                send_alert(self.env, subject, body)
        except Exception:                                   # noqa: BLE001
            log.warning("alert failed: %s", subject, exc_info=True)

    def _on_progress(self, event: dict[str, Any]) -> None:
        """The driver's phase-by-phase report, turned into a heartbeat."""
        kind = str(event.get("event") or "")
        now = _now()
        if kind == "phase_start":
            self._beat(state="running", phase=event.get("phase"),
                       model=event.get("model"), effort=event.get("effort"),
                       phase_started_at=now, turns=0, last_error="")
        elif kind == "phase_end":
            self._beat(phase_done=event.get("phase"),
                       phase_ok=bool(event.get("ok")),
                       last_phase_turns=event.get("num_turns"),
                       last_phase_limit=event.get("limit"))
        elif kind == "gate":
            self._beat(gate=("approved" if event.get("approved")
                             else "escalated" if event.get("approved") is None
                             else "declined"))
        elif kind == "stopped":
            self._beat(state="stopping", last_outcome="needs_human",
                       last_reason=str(event.get("reason") or "")[:600])
        elif kind == "finished":
            self._beat(state="finishing",
                       last_outcome=event.get("outcome"),
                       last_reason=str(event.get("reason") or "")[:600])

    def _live_beat(self, slug: str) -> None:
        """The timer's heartbeat: what the running phase has done so far."""
        from galley.driver import live_progress

        phase = self._status.get("phase")
        try:
            live = live_progress(self.root / slug, phase)
        except Exception:                                   # noqa: BLE001
            live = {}
        self._beat(**live)

    def _ticker(self, slug: str, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_interval_s):
            self._live_beat(slug)


    def poll_once(self) -> RunReport:
        """Look once, and run at most one book."""
        report = RunReport()
        books, error = poll_awaiting(self.env, opener=self.opener)
        self._poll_health(error)
        report.looked_at = len(books)
        ledger = self.ledger()

        # A person explicitly cleared this book's processed flag. Archive the
        # old ledger entry before retrying deliveries, so stale hand-offs cannot
        # complete the new request. A reset gets its own workspace below.
        for book in books:
            old = ledger.claimed(book.file_id)
            if book.request_id and old.get("request_id", "") != book.request_id:
                history = list(old.get("previous_runs") or [])
                if old:
                    history.append({k: v for k, v in old.items() if k != "previous_runs"})
                ledger.books[book.file_id] = {"request_id": book.request_id,
                                              "previous_runs": history}
                ledger.save()

        # Retry pending delivery before starting another book.
        self.retry_deliveries(ledger, report)

        if self._halt and not self._token_recovered():
            # A dead token would turn every awaiting book into needs_human,
            # one per poll. Hold the queue instead, and say so.
            report.halted = self._halt
            self._beat(state="halted", awaiting=len(books),
                       pending_deliveries=len(ledger.pending_deliveries()),
                       credentials_error=self._halt[:600])
            if books:
                self.log(f"{len(books)} book(s) awaiting; holding them until "
                         f"the subscription token works again.")
            return report

        if self._usage_waiting():
            report.halted = str(self._usage_pause().get("reason")
                                or self._halt or "Claude usage limit")
            return report

        for book in books:
            state = ledger.state(book.file_id)
            if state in (FINISHED, FAILED, PENDING_DELIVERY):
                report.skipped.append(f"{book.name} ({state})")
                continue
            if state == CLAIMED and self.held_for_code(book.file_id, ledger):
                report.skipped.append(f"{book.name} (held for new code)")
                continue
            resume = state == CLAIMED
            self.run_book(book, ledger, report, resume=resume)
            return report                     # one book at a time, on purpose
        if books:
            self.log(f"{len(books)} book(s) awaiting; all already handled here.")
        self._beat(state="idle", awaiting=len(books),
                   pending_deliveries=len(ledger.pending_deliveries()))
        return report

    def _poll_health(self, error: str) -> None:
        """Alert once when polling breaks, and once when it recovers — not on
        every five-minute retry in between."""
        now = _now()
        if error:
            first = not self._poll_error
            self._poll_error = error
            self._beat(last_poll_at=now, last_poll_error=error)
            if first:
                self._alarm(
                    f"cannot reach DocProof from {self.host}",
                    f"The Galley agent on {self.host} could not ask "
                    f"{self.env.awaiting_url} for books:\n\n  {error}\n\n"
                    f"It keeps retrying every "
                    f"{self.poll_interval_s / 60:.0f} minutes and will say "
                    f"when it gets through again. Until then no book is "
                    f"picked up. Check DOCPROOF_AGENT_TOKEN on both sides, "
                    f"and that the app is up.")
            return
        if self._poll_error:
            self._alarm(f"DocProof is reachable again from {self.host}",
                        f"The Galley agent on {self.host} is polling "
                        f"{self.env.awaiting_url} normally again (the last "
                        f"failure was: {self._poll_error}).")
        self._poll_error = ""
        self._beat(last_poll_at=now, last_poll_error="")

    def _reload_env(self) -> None:
        """Pick up a rotated token from the credentials file, if there is one
        (on Fly the file is rewritten at boot; on a Mac it is edited by hand)."""
        path = self.env.path
        if not path:
            return
        try:
            self.env = read_env(path)
        except AgentError as e:
            self.log(f"credentials file not reloaded: {e}")

    def _usage_pause(self) -> dict[str, Any]:
        import math
        try:
            data = json.loads((self.root / USAGE_PAUSE_NAME).read_text("utf-8"))
            until = data.get("resume_after") if isinstance(data, dict) else None
            if type(until) in (int, float) and math.isfinite(until):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _usage_beat(self, pause: dict[str, Any]) -> None:
        reason = str(pause.get("reason") or "Claude subscription usage limit")
        self._beat(state="halted", phase=None, credentials_error="",
                   usage_limit=reason[:800], last_error=reason[:800],
                   usage_resets_at=datetime.fromtimestamp(
                       pause["resume_after"], timezone.utc).isoformat(),
                   last_outcome="held", last_reason=reason[:800])

    def pause_for_usage(self, reason: str, *, book: str = "", slug: str = "") -> None:
        from docproof.utils.files import write_atomic
        now = self.wall_clock()
        pause = {"reason": reason, "recorded_at": now,
                 "resume_after": resume_after(reason, now=now),
                 "book": book, "slug": slug}
        write_atomic(self.root / USAGE_PAUSE_NAME, json.dumps(pause, indent=2))
        self._halt = ""  # A quota is not a revoked token.
        self._usage_beat(pause)
        self.log(f"Claude usage limit: queue paused until "
                 f"{self._status['usage_resets_at']}; checkpoints preserved.")

    def _usage_waiting(self) -> bool:
        pause = self._usage_pause()
        if not pause:
            return False
        if self.wall_clock() < pause["resume_after"]:
            self._usage_beat(pause)
            return True
        # Only one availability check after the deadline, never a whole book
        # as a quota probe. A continuing limit establishes another cooldown.
        self._reload_env()
        error = self._preflight() if self.preflight is not None else ""
        if is_usage_limited(error):
            self.pause_for_usage(error)
            return True
        (self.root / USAGE_PAUSE_NAME).unlink(missing_ok=True)
        self._beat(usage_limit="", usage_resets_at="", last_error="")
        if error:
            self.halt(error)
            return True
        self.log("Claude subscription is available again; resuming checkpointed work.")
        return False

    def _token_recovered(self) -> bool:
        """While halted: re-read the credentials and try to sign in. Without
        a preflight the next claim is the test."""
        self._reload_env()
        if self.preflight is not None:
            error = self._preflight()
            if error:
                if is_usage_limited(error):
                    self.pause_for_usage(error)
                    return True
                if error != self._halt:
                    self._halt = error
                    self.log(f"still halted: {error}")
                return False
        self._alarm(f"Galley agent on {self.host} is signed in again",
                    f"The subscription token on {self.host} works again; "
                    f"the held books are picked up from this poll on "
                    f"(the last failure was: {self._halt}).")
        self._halt = ""
        self._beat(credentials_error="", last_error="")
        return True

    def _preflight(self) -> str:
        try:
            return str(self.preflight(dict(self.env.values)) or "")
        except Exception as e:                              # noqa: BLE001
            log.exception("The sign-in check itself failed")
            return f"the sign-in check crashed: {e}"

    def halt(self, reason: str, *, book: str = "", slug: str = "") -> None:
        """Stop claiming books because the subscription token is rejected.
        Alerts once per failure, not once per poll."""
        first = not self._halt
        self._halt = reason
        fields: dict[str, Any] = {"state": "halted", "phase": None,
                                  "credentials_error": reason[:600],
                                  "last_error": reason[:400]}
        if book:
            fields.update(last_book=book, held_book=book, held_slug=slug,
                          last_outcome="held", last_reason=reason[:600])
        self._beat(**fields)
        self.log(f"HALTED: {reason[:300]}")
        if first:
            held = (f"{book} is claimed and untouched; it resumes from the "
                    f"same phase once the token works.\n\n" if book else "")
            self._alarm(
                f"Galley agent on {self.host}: subscription token rejected",
                f"Claude Code on {self.host} could not sign in:\n\n  "
                f"{reason[:800]}\n\n{held}No book is claimed until the "
                f"token is replaced; nothing has been marked needs_human "
                f"over this.\n\n{TOKEN_FIX_HINT}")

    def run_forever(self) -> None:
        self.log(f"Galley agent: polling {self.env.awaiting_url} every "
                 f"{self.poll_interval_s / 60:.0f} min.")
        self.announce()
        while True:
            try:
                self.poll_once()
            except Exception as e:                          # noqa: BLE001
                # Keep polling after unexpected failures.
                log.exception("The poll failed; trying again next interval.")
                self._beat(state="idle", last_error=f"poll crashed: {e}"[:400])
            self.sleep(self.poll_interval_s)

    def announce(self) -> None:
        """Boot: the first heartbeat and a one-line email, so a machine that
        came up (or came back after a deploy) is noticed."""
        from docproof import __version__

        started = _now()
        ledger = self.ledger()
        pending = ledger.pending()
        self._beat(state="starting", started_at=started, awaiting=0,
                   pending_deliveries=len(ledger.pending_deliveries()),
                   last_error="")
        # Find out now whether the token signs in, not after a book's first
        # phase has been written off.
        if self._usage_pause():
            # The first poll honors/checks the persisted deadline. No token
            # probe is needed while the same subscription is cooling down.
            self._usage_beat(self._usage_pause())
            return
        problem = self._preflight() if self.preflight is not None else ""
        if is_usage_limited(problem):
            self.pause_for_usage(problem)
            return
        self._alarm(
            f"Galley agent started on {self.host}"
            + (" — but its token is rejected" if problem else ""),
            f"DocProof {__version__} on {self.host} is polling "
            f"{self.env.awaiting_url} every {self.poll_interval_s / 60:.0f} "
            f"minutes for books to proofread.\n"
            f"Workspaces: {self.root}\n"
            + (f"{len(pending)} book(s) were claimed but unfinished; the "
               f"first poll resumes them.\n" if pending else "")
            + (f"\nThe subscription token does not sign in ({problem}). No "
               f"book is claimed until it is replaced. {TOKEN_FIX_HINT}\n"
               if problem else "")
            + "Progress shows under Admin → Automations → Proofread.")
        if problem:
            self._halt = problem
            self._beat(state="halted", credentials_error=problem[:600],
                       last_error=problem[:400])


    def run_book(self, book: AwaitingBook, ledger: Ledger, report: RunReport,
                 *, resume: bool = False) -> None:
        """Record the claim before downloading, then run the driver and record
        its outcome.
        """
        slug = slug_for(book.name, book.author_last, book.file_id)
        if book.request_id:
            slug += "-r" + hashlib.sha256(book.request_id.encode()).hexdigest()[:12]
        report.claimed = book.name
        folder = self.drive_folder_override or book.folder_id
        ledger.record(book.file_id, CLAIMED, name=book.name, slug=slug,
                      folder_id=folder, request_id=book.request_id,
                      operational_status="", reason="")
        self.log(f"{'Resuming' if resume else 'Claiming'} {book.name} "
                 f"(workspace {slug}).")
        self._status = {k: v for k, v in self._status.items()
                        if k in ("started_at", "last_poll_at",
                                 "last_poll_error")}
        self._beat(state="running", book=book.name, slug=slug,
                   resumed=resume, run_started_at=_now(), phase=None)

        try:
            local = self.fetch_book(book)
        except Exception as e:                              # noqa: BLE001
            log.exception("Could not download %s", book.name)
            self._operational_block(book, ledger, report, slug, folder,
                                    f"DocProof could not download {book.name} "
                                    f"from Drive ({e}); the proofread has not started.")
            return

        self._file_id = book.file_id
        try:
            result = self.drive_book(local, slug, folder, resume=resume)
        except UsageLimitError as e:
            report.outcome, report.reason = "held", str(e)
            ledger.record(book.file_id, CLAIMED, name=book.name, slug=slug,
                          folder_id=folder, operational_status="waiting_for_usage",
                          reason=str(e)[:800])
            self.pause_for_usage(str(e), book=book.name, slug=slug)
            return
        except CredentialsError as e:
            # The token, not the book. The claim stands and the run resumes
            # from the same phase once the token is replaced.
            report.outcome = "held"
            report.reason = str(e)
            self.halt(str(e), book=book.name, slug=slug)
            return
        except Exception as e:                              # noqa: BLE001
            log.exception("The proofread of %s crashed", book.name)
            self._operational_block(book, ledger, report, slug, folder,
                                    f"The proofreading run over {book.name} "
                                    f"crashed ({e}); recovery is pending.")
            return

        outcome = getattr(result, "outcome", "needs_human")
        reason = getattr(result, "reason", "")
        report.outcome, report.reason = outcome, reason
        if outcome == "blocked" and getattr(result, "recovery_exhausted", False):
            self._hold_for_new_code(book, ledger, report, slug, folder, reason)
            return
        if outcome == "blocked":
            # Infrastructure, missing evidence, or pending Astra repairs are
            # not a human-proofreader verdict. Keep the claim for safe resume.
            package_path = self.root / slug / "runs" / "driver" / "package.json"
            if (getattr(result, "stopped_at", None) == "deliver" and folder
                    and package_path.is_file() and getattr(result, "handoff", None)):
                package = json.loads(package_path.read_text("utf-8"))
                ledger.record(
                    book.file_id, PENDING_DELIVERY, name=book.name, slug=slug,
                    folder_id=folder, outcome=package["outcome"], reason=package["reason"],
                    handoff_files=[item["path"] for item in package["artifacts"]],
                    verified_publication=True, delivery_attempts=1,
                    next_delivery_at=time.time() + self.poll_interval_s,
                    delivery_error=reason[:300], uploaded_names={})
                self._beat(state="idle", phase=None, last_outcome="blocked",
                           last_reason=reason[:600], last_book=book.name,
                           delivery="pending")
            else:
                self._operational_block(book, ledger, report, slug, folder, reason)
            return
        uploaded = list(getattr(result, "uploaded", []) or [])
        handoff = [Path(p) for p in (getattr(result, "handoff", []) or [])]
        if folder and handoff and not uploaded:
            package_path = self.root / slug / "runs" / "driver" / "package.json"
            if package_path.is_file():
                package = json.loads(package_path.read_text("utf-8"))
                ledger.record(
                    book.file_id, PENDING_DELIVERY, name=book.name, slug=slug,
                    folder_id=folder, outcome=package["outcome"], reason=package["reason"],
                    handoff_files=[item["path"] for item in package["artifacts"]],
                    verified_publication=True, delivery_attempts=1,
                    next_delivery_at=time.time() + self.poll_interval_s,
                    delivery_error="The verified driver upload is pending.", uploaded_names={})
                self._beat(state="idle", phase=None, last_outcome=outcome,
                           last_reason=reason[:600], last_book=book.name, delivery="pending")
                return
            # Queue a failed handoff upload before marking the book
            # finished.
            self.owe_delivery(book, ledger, slug, folder, outcome, reason,
                              handoff, why="the driver's upload failed")
            self._beat(state="idle", phase=None, last_outcome=outcome,
                       last_reason=reason[:600], last_book=book.name,
                       delivery="pending")
            return
        ledger.record(book.file_id, FINISHED if outcome == "done" else FAILED,
                      name=book.name, slug=slug, folder_id=folder,
                      outcome=outcome, reason=reason[:400],
                      uploaded=uploaded)
        self.log(f"{book.name}: {outcome} — {reason[:200]}")
        self._beat(state="idle", phase=None, last_outcome=outcome,
                   last_reason=reason[:600], last_book=book.name,
                   finished_at=_now(), delivery="uploaded" if uploaded
                   else "none")

    def _operational_block(self, book: AwaitingBook, ledger: Ledger,
                           report: RunReport, slug: str, folder: str,
                           reason: str) -> None:
        report.outcome, report.reason = "blocked", reason
        ledger.record(book.file_id, CLAIMED, name=book.name, slug=slug,
                      folder_id=folder, operational_status="blocked",
                      reason=reason[:400])
        self._beat(state="idle", phase=None, last_outcome="blocked",
                   last_reason=reason[:600], last_book=book.name,
                   delivery="pending")

    def _hold_for_new_code(self, book: AwaitingBook, ledger: Ledger,
                           report: RunReport, slug: str, folder: str,
                           reason: str) -> None:
        """Preserve a concrete failure after bounded automatic recovery.

        Local questions never reach this path. Do not spend on the same
        exhausted operation at every poll; a new release can resume it.
        """
        from docproof import __version__
        report.outcome, report.reason = "blocked", reason
        ledger.record(book.file_id, CLAIMED, name=book.name, slug=slug,
                      folder_id=folder, operational_status=HELD_FOR_CODE,
                      held_version=code_id(), reason=reason[:400])
        self._beat(state="idle", phase=None, last_outcome="blocked",
                   last_reason=reason[:600], last_book=book.name,
                   delivery="pending")
        self._alarm(f"{book.name}: automatic recovery could not finish",
                    f"Galley could not complete a required operation on "
                    f"version {__version__} within its recovery limits. "
                    f"The evidence and checkpoints are preserved. No answer "
                    f"to a question is expected. The book remains claimed "
                    f"and becomes eligible to resume after a new deployment."
                    f"\n\n{reason[:1500]}")
        self.log(f"{book.name}: held for new code (v{__version__}).")

    def held_for_code(self, file_id: str, ledger: Ledger) -> bool:
        """Whether a claimed book is waiting for a version other than the one
        it stopped on."""
        entry = ledger.claimed(file_id)
        return (entry.get("operational_status") == HELD_FOR_CODE
                and entry.get("held_version") == code_id())

    def fetch_book(self, book: AwaitingBook) -> Path:
        """The Book 1, on this Mac, as a .docx."""
        dest = self.root / DOWNLOAD_DIR / book.file_id
        if self.download is not None:
            return self.download(book, dest)
        from app.watch.proof import fetch
        from galley.driver import drive_token

        token = drive_token()
        # fetch picks download versus native-Doc export from the handle's
        # type, and also converts .doc/.odt.
        return fetch(token, drive_handle(token, book), dest)

    def drive_book(self, local: Path, slug: str, folder_id: str, *,
                   resume: bool) -> Any:
        """Run the practitioner loop over one manuscript, in this process."""
        kwargs: dict[str, Any] = {}
        if self.budget_usd is not None:
            kwargs["budget_usd"] = self.budget_usd
        if self.verify_upload is not None:
            kwargs["verify_upload"] = self.verify_upload
        if resume:
            # Resume claimed work from its recorded state.
            start = self.resume_phase(slug)
            if start:
                kwargs["start_phase"] = start
                self.log(f"Resuming {slug} from the {start} phase.")
        runner = self.run_driver or _run_driver
        # The driver reports every phase boundary; a timer fills the minutes
        # in between with what the running session has done so far.
        stop = threading.Event()
        ticker = None
        if self.heartbeat_interval_s > 0:
            ticker = threading.Thread(target=self._ticker, args=(slug, stop),
                                      name=f"galley-heartbeat-{slug}",
                                      daemon=True)
            ticker.start()
        try:
            return runner(book=local, slug=slug, workspace_root=self.root,
                          drive_folder_id=folder_id, source_id=self._file_id,
                          env=self.driver_env(), upload=self.upload,
                          progress=self._on_progress, **kwargs)
        finally:
            stop.set()
            if ticker is not None:
                ticker.join(timeout=5)

    def resume_phase(self, slug: str) -> str:
        """Choose the next phase from the recorded run state and driver phase
        order.
        """
        from galley.driver import MECHANICAL_PHASES, REQUIRED_STATE
        from galley.state_machine import RunStateMachine

        path = self.root / slug / "state.json"
        if not path.is_file():
            return ""
        try:
            current = RunStateMachine.load(path).current
        except (OSError, ValueError):
            return ""
        if not current:
            return ""
        done = [phase for phase in MECHANICAL_PHASES
                if REQUIRED_STATE.get(phase)
                and _state_index(REQUIRED_STATE[phase])
                <= _state_index(current)]
        if not done:
            return ""
        last = done[-1]
        after = MECHANICAL_PHASES.index(last) + 1
        # A crash after delivered but before the agent's completion ledger must
        # retry only delivery, never fall back to profile and repeat the book.
        return MECHANICAL_PHASES[after] if after < len(MECHANICAL_PHASES) else "deliver"

    def driver_env(self) -> dict[str, str]:
        """Combine the process environment and credential file, supplying
        service PATH and HOME defaults. The driver strips API keys before
        spawning sessions.
        """
        env = dict(os.environ)
        env.setdefault("PATH", PATH)
        env.update(self.env.values)
        return env

    def give_up(self, book: AwaitingBook, ledger: Ledger, report: RunReport,
                slug: str, folder_id: str, reason: str) -> None:
        """Write a needs_human outcome and attempt delivery. Failed delivery is
        retried without rerunning the proofread.
        """
        report.outcome, report.reason = "needs_human", reason
        self._beat(state="idle", phase=None, last_outcome="needs_human",
                   last_reason=reason[:600], last_book=book.name,
                   finished_at=_now(), last_error=reason[:400])
        try:
            files = self.write_failure(slug, book.name, reason)
        except Exception as e:                              # noqa: BLE001
            log.exception("Could not write the failure verdict for %s",
                          book.name)
            ledger.record(book.file_id, FAILED, name=book.name, slug=slug,
                          folder_id=folder_id, outcome="needs_human",
                          reason=reason[:400], uploaded=[],
                          delivery="unwritten", delivery_error=str(e)[:300])
            self.log(f"{book.name} failed and its verdict could not even be "
                     f"written ({e}) — DocWatch will keep waiting on this "
                     f"book until somebody looks at it.")
            return
        if not folder_id or not files:
            ledger.record(book.file_id, FAILED, name=book.name, slug=slug,
                          folder_id=folder_id, outcome="needs_human",
                          reason=reason[:400], uploaded=[])
            self.log(f"{book.name}: needs_human — {reason[:200]}")
            return
        self.owe_delivery(book, ledger, slug, folder_id, "needs_human",
                          reason, files, why="")


    def owe_delivery(self, book: AwaitingBook, ledger: Ledger, slug: str,
                     folder_id: str, outcome: str, reason: str,
                     files: list[Path], *, why: str) -> None:
        """Attempt delivery and save confirmed uploads. On failure, record the
        remaining files for a later poll to retry.
        """
        entry = ledger.claimed(book.file_id)
        uploaded_names = dict(entry.get("uploaded_names") or {})
        ok = self._upload_missing(files, folder_id, uploaded_names)
        if ok:
            ledger.record(book.file_id, FINISHED if outcome == "done" else FAILED,
                          name=book.name, slug=slug, folder_id=folder_id,
                          outcome=outcome, reason=reason[:400],
                          uploaded=list(uploaded_names.values()),
                          uploaded_names=uploaded_names, delivery="delivered")
            self.log(f"{book.name}: {outcome} — {reason[:200]}")
            return
        attempts = int(entry.get("delivery_attempts") or 0) + 1
        wait = self.poll_interval_s * (DELIVERY_BACKOFF_BASE ** (attempts - 1))
        ledger.record(book.file_id, PENDING_DELIVERY, name=book.name,
                      slug=slug, folder_id=folder_id, outcome=outcome,
                      reason=reason[:400],
                      handoff_files=[str(p) for p in files],
                      uploaded_names=uploaded_names,
                      uploaded=list(uploaded_names.values()),
                      delivery_attempts=attempts,
                      next_delivery_at=time.time() + wait,
                      delivery_error=self._last_delivery_error[:300])
        self.log(f"{book.name}: {outcome} — the verdict is written but "
                 f"{len(files) - len(uploaded_names)} hand-off file(s) could "
                 f"not be uploaded{f' ({why})' if why else ''}; delivery "
                 f"will be retried (attempt {attempts} of "
                 f"{MAX_DELIVERY_ATTEMPTS}).")

    def _upload_missing(self, files: list[Path], folder_id: str,
                        uploaded_names: dict[str, str]) -> bool:
        """Upload unconfirmed files and update name-to-id mappings. Return true
        only when every expected file has an upload id.
        """
        self._last_delivery_error = ""
        if not files:
            self._last_delivery_error = "No hand-off files were recorded."
            return False
        uploader = self.upload or _default_upload
        for path in files:
            if uploaded_names.get(path.name):
                continue
            if not path.is_file():
                log.warning("hand-off file %s is missing; skipped", path)
                self._last_delivery_error = f"Hand-off file {path.name} is missing."
                return False
            try:
                ids = uploader([path], folder_id)
            except Exception as e:                          # noqa: BLE001
                log.warning("upload of %s failed: %s", path.name, e)
                self._last_delivery_error = str(e)
                return False
            if (not ids or len(ids) != 1 or not isinstance(ids[0], str)
                    or not ids[0].strip()):
                self._last_delivery_error = f"No upload id returned for {path.name}."
                return False
            uploaded_names[path.name] = ids[0]
        return all(uploaded_names.get(p.name) for p in files)

    _last_delivery_error: str = ""
    _file_id: str = ""

    def retry_deliveries(self, ledger: Ledger, report: RunReport,
                         *, now: float | None = None) -> None:
        """Retry due deliveries with exponential backoff; abandon them after
        MAX_DELIVERY_ATTEMPTS.
        """
        clock = time.time() if now is None else now
        for file_id in ledger.pending_deliveries():
            entry = ledger.claimed(file_id)
            if float(entry.get("next_delivery_at") or 0) > clock:
                continue
            files = [Path(p) for p in (entry.get("handoff_files") or [])]
            folder = str(entry.get("folder_id") or "")
            name = str(entry.get("name") or file_id)
            outcome = str(entry.get("outcome") or "needs_human")
            attempts = int(entry.get("delivery_attempts") or 0)
            if attempts >= MAX_DELIVERY_ATTEMPTS:
                ledger.record(file_id, FAILED, delivery="abandoned")
                self.log(f"{name}: delivery abandoned after {attempts} "
                         f"attempt(s) — put {len(files)} hand-off file(s) in "
                         f"folder {folder} by hand.")
                report.skipped.append(f"{name} (delivery abandoned)")
                self._alarm(
                    f"{name}: hand-off could not be delivered",
                    f"The proofread of {name} finished ({outcome}) but its "
                    f"hand-off could not be uploaded to Drive folder {folder} "
                    f"in {attempts} attempts, so the agent has stopped "
                    f"trying. The files are on {self.host} under "
                    f"{self.root / str(entry.get('slug') or '')}/handoff/:\n"
                    + "".join(f"  - {f.name}\n" for f in files)
                    + f"Last error: {entry.get('delivery_error') or '?'}\n"
                    f"DocWatch is still waiting on this book.")
                self._beat(last_error=f"{name}: delivery abandoned")
                continue
            uploaded_names = dict(entry.get("uploaded_names") or {})
            if entry.get("verified_publication"):
                ok = self._retry_verified_publication(entry, folder, uploaded_names)
            else:
                ok = self._upload_missing(files, folder, uploaded_names)
            if ok:
                ledger.record(file_id, FINISHED if outcome == "done" else FAILED,
                              uploaded=list(uploaded_names.values()),
                              uploaded_names=uploaded_names,
                              delivery="delivered")
                self.log(f"{name}: delivered on retry {attempts + 1} "
                         f"({outcome}).")
                report.delivered.append(name)
                continue
            attempts += 1
            wait = self.poll_interval_s * (DELIVERY_BACKOFF_BASE ** (attempts - 1))
            ledger.record(file_id, PENDING_DELIVERY,
                          uploaded_names=uploaded_names,
                          uploaded=list(uploaded_names.values()),
                          delivery_attempts=attempts,
                          next_delivery_at=clock + wait,
                          delivery_error=self._last_delivery_error[:300])
            self.log(f"{name}: delivery retry {attempts} failed; next in "
                     f"{wait / 60:.0f} min.")

    def _retry_verified_publication(self, entry: dict[str, Any], folder: str,
                                    uploaded_names: dict[str, str]) -> bool:
        """Use the same durable, verified uploader as the default driver."""
        from galley.astra_review import validate_receipt
        from galley.driver import publish_verified_handoff
        from galley.manifest import sha256_file
        from galley.state_machine import RunStateMachine
        from galley.verify import deliverable_docx
        ws = self.root / str(entry["slug"])
        driver_dir = ws / "runs" / "driver"
        try:
            package = json.loads((driver_dir / "package.json").read_text("utf-8"))
            run = Path(package["run"])
            receipt = validate_receipt(run)
            manuscript = deliverable_docx(run)
            human = (package.get("kind") == "human_review"
                     and receipt["review"]["editorial_verdict"] == "needs_human")
            if ((not receipt.get("delivery_ready") and not human) or manuscript is None
                    or receipt["packet_sha256"] != package["packet_sha256"]
                    or sha256_file(manuscript) != package["build_sha256"]):
                raise AgentError("The pending package no longer matches its reviewed build.")
            publish_verified_handoff(
                package, folder, driver_dir / "delivery.json",
                source_id=package["source_id"], upload=self.upload,
                verify=self.verify_upload)
            machine = RunStateMachine.load(ws / "state.json")
            if not machine.reached("delivered"):
                previous = machine.history[-1]
                machine.advance("delivered", by="galley verified delivery retry",
                                source_sha256=machine.source_sha256,
                                config_sha256=previous.config_sha256)
                machine.save(ws / "state.json")
            return True
        except Exception as e:                              # noqa: BLE001
            self._last_delivery_error = str(e)
            return False
        finally:
            try:
                state = json.loads((driver_dir / "delivery.json").read_text("utf-8"))
                uploaded_names.update({name: row["file_id"] for name, row in
                                       state.get("artifacts", {}).items()
                                       if row.get("verified")})
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def write_failure(self, slug: str, source_name: str,
                      reason: str) -> list[Path]:
        """The hand-off for a book that never ran: a verdict, and the decision
        log if there is anything to log."""
        from galley.driver import (build_diagnostics, build_handoff,
                                   handoff_base)
        from galley.journal import write_journal
        from galley.outcome import Outcome, hubspot_fields

        ws = self.root / slug
        runs = ws / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        Outcome(outcome="needs_human", reason=reason,
                evidence={"agent": True, "slug": slug},
                hubspot=hubspot_fields("needs_human"),
                set_by="galley agent").save(runs)
        try:
            write_journal(runs, ws / "deliverable" / "DECISION_LOG.md",
                          workspace=ws, book=source_name, generated_at=_now())
        except Exception as e:                              # noqa: BLE001
            log.warning("No decision log for %s (%s)", slug, e)
        out = ws / "handoff"
        try:
            files = build_handoff(ws, source_name, out,
                                  outcome_sources=[runs / "outcome.json"],
                                  partial=True)
        except Exception:                                   # noqa: BLE001
            # If a partial handoff cannot be built, deliver the outcome
            # alone.
            out.mkdir(parents=True, exist_ok=True)
            import shutil
            dest = out / f"{handoff_base(source_name)} - outcome.json"
            shutil.copy2(runs / "outcome.json", dest)
            files = [dest]
        # The evidence rides with the verdict: whatever the driver logged
        # before it gave up, plus the agent's own log.
        bundle = build_diagnostics(
            ws, source_name, out,
            extra=[(self.root / LOG_NAME, "agent.log")])
        if bundle is not None:
            files.append(bundle)
        return files


    def forget(self, key: str) -> str:
        """Drop one book from the ledger, by Drive id or file name, so the
        next poll claims it as if it had never been seen. The workspace is
        left in place; the driver reseeds it. Returns the book's name."""
        ledger = self.ledger()
        wanted = key.strip()
        matches = [fid for fid, entry in ledger.books.items()
                   if fid == wanted or str(entry.get("name") or "") == wanted]
        if not matches:
            known = ", ".join(str(e.get("name") or fid)
                              for fid, e in ledger.books.items()) or "nothing"
            raise AgentError(f"No book in the ledger is {wanted!r}; the "
                             f"ledger holds: {known}.")
        if len(matches) > 1:
            raise AgentError(f"{wanted!r} names {len(matches)} ledger entries; "
                             f"use the Drive id: {', '.join(matches)}.")
        entry = ledger.books.pop(matches[0])
        ledger.save()
        name = str(entry.get("name") or matches[0])
        self.log(f"Forgot {name} ({matches[0]}, was {entry.get('state')}).")
        return name

    def status(self) -> dict[str, Any]:
        ledger = self.ledger()
        return {"workspace_root": str(self.root),
                "ledger": str(self.ledger_path),
                "app": self.env.awaiting_url,
                "books": ledger.books,
                "pending": ledger.pending(),
                "pending_deliveries": ledger.pending_deliveries()}


def _state_index(state: str) -> int:
    from galley.state_machine import RUN_STATES
    return RUN_STATES.index(state) if state in RUN_STATES else -1


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_BY_SUFFIX = {".docx": _DOCX, ".doc": "application/msword",
                   ".odt": "application/vnd.oasis.opendocument.text"}


def drive_handle(token: str, book: AwaitingBook, *, opener=None):
    """The Book 1 as a DriveFile carrying Drive's own type for it.

    The name alone cannot tell a native Google Doc from a Word upload whose
    extension was dropped — both read "Smith - Book 1" — and exporting the
    Word file is a 403 ("Export only supports Docs Editors files") that
    blocks the book on every poll. Only when Drive will not say does the
    name's guess stand in."""
    from app.watch import drive

    try:
        meta = drive.get_file(token, book.file_id,
                              opener=opener or drive._open_url)
        mime = meta.mime_type
    except drive.DriveError as e:
        log.warning("Could not read Drive's type for %s (%s); guessing "
                    "from the name.", book.name, e)
        mime = ""
    return drive.DriveFile(id=book.file_id, name=book.name,
                           mime_type=mime or _mime_for(book.name))


def _mime_for(name: str) -> str:
    """Infer MIME type from the filename; assume an extensionless name is a
    native Google Doc.
    """
    suffix = Path(name).suffix.lower()
    if not suffix:
        from app.watch.drive import GOOGLE_DOC_MIME
        return GOOGLE_DOC_MIME
    return _MIME_BY_SUFFIX.get(suffix, _DOCX)


def _run_driver(**kwargs: Any) -> Any:
    """Run the driver in-process and return its DriveResult."""
    from galley.driver import Driver

    upload = kwargs.pop("upload", None)
    kwargs.setdefault("on_source_change", "revise")
    # The driver's phase banners go through the logger, not print(): under a
    # service manager stdout is block-buffered and a "--- phase settle ---"
    # line would surface hours late, after the run.
    kwargs.setdefault("log", lambda message: log.info("%s", message))
    driver = Driver(approve="auto", mechanical_only=True, **kwargs)
    if upload is not None:
        driver.upload = upload
    # A missing final-review login is an operational setup problem. Detect it
    # before spending Claude allowance on a book that cannot finish its handoff.
    if driver.astra_review:
        from galley.driver import astra_review_settings
        settings = astra_review_settings(driver._final_run() or driver.workspace,
                                         transport=driver.astra_transport,
                                         max_chunk_bytes=driver.astra_chunk_bytes)
        if settings["transport"] == "codex":
            from galley.codex_runner import check_login
            check_login()
    # Format before seed_workspace records the source hash and before any
    # paragraph ids, findings or verification evidence are created. The intake
    # preserves the Book 1 filename and caches the verified bytes on resume.
    from galley.intake import DEFAULT_MODEL as formatting_model, format_for_proof
    from galley.manifest import sha256_file
    from galley.state_machine import RunStateMachine

    driver._progress("phase_start", phase="formatting",
                     model=formatting_model, effort=None)
    driver.log("Formatting Book 1 before proofreading.")
    try:
        formatted = format_for_proof(driver.book, driver.workspace,
            progress=lambda done, total: driver.log(
                f"Formatting: labelled window {done} of {total}."))
    except Exception:
        driver._progress("phase_end", phase="formatting", ok=False)
        raise
    state_path = driver.workspace / "state.json"
    if state_path.is_file():
        previous = RunStateMachine.load(state_path)
        if previous.source_sha256 != sha256_file(formatted):
            # A changed Book 1 creates a new revision; its proofread starts
            # at profile, never at the old manuscript's interrupted phase.
            driver.start_phase = None
    driver.book = formatted
    driver._progress("phase_end", phase="formatting", ok=True)
    return driver.run()


def _default_upload(files: list[Path], folder_id: str) -> list[str]:
    from galley.driver import _default_upload as upload
    return upload(files, folder_id)


# Platform service installers.

def is_linux(platform: str | None = None) -> bool:
    return (platform or sys.platform).startswith("linux")


def executable() -> str:
    """Find an absolute docproof executable path, preferring the currently
    invoked copy.
    """
    import shutil

    argv0 = Path(sys.argv[0])
    if argv0.name == "docproof" and argv0.exists():
        return str(argv0.resolve())
    found = shutil.which("docproof")
    if found:
        return str(Path(found).resolve())
    raise AgentError(
        "Could not find the `docproof` command to schedule. Install DocProof "
        "with `pip install -e .` in its folder and try again.")


def program(*, workspace_root: Path, env_file: Path,
            poll_interval_s: float) -> list[str]:
    """Build the service command with explicit workspace, credential, and
    polling options.
    """
    return [executable(), "galley", "agent",
            "--workspace-root", str(workspace_root),
            "--env-file", str(env_file),
            "--poll-interval", str(int(poll_interval_s))]



def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def plist_content(*, command: list[str], log_path: Path,
                  workspace_root: Path) -> bytes:
    """Build a launchd definition that starts at login, restarts the poller,
    and writes agent.log.
    """
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        # Throttle crash restarts to once per minute.
        "ThrottleInterval": 60,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "WorkingDirectory": str(workspace_root),
        "EnvironmentVariables": {"PATH": PATH, "HOME": str(Path.home())},
        "ProcessType": "Background",
    })


def _install_launchd(*, command: list[str], workspace_root: Path,
                     run, path: Path | None) -> Path:
    target = path or plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(plist_content(command=command,
                                     log_path=workspace_root / LOG_NAME,
                                     workspace_root=workspace_root))
    domain = f"gui/{os.getuid()}"
    # Unload the previous definition; a missing service is normal on first
    # install.
    run(["launchctl", "bootout", f"{domain}/{LABEL}"],
        capture_output=True, text=True)
    result = run(["launchctl", "bootstrap", domain, str(target)],
                 capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise AgentError(
            f"macOS would not start the agent{': ' + detail if detail else '.'}"
            f" It is written at {target}; `launchctl bootstrap {domain} "
            f"{target}` is what failed.")
    return target


def _uninstall_launchd(*, run, path: Path | None) -> bool:
    target = path or plist_path()
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True)
    if not target.exists():
        return False
    target.unlink()
    return True



# The systemd unit name.
UNIT_NAME = "galley-agent.service"


def units_dir() -> Path:
    """Where a user unit lives, honouring XDG_CONFIG_HOME when it is set."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def unit_path() -> Path:
    return units_dir() / UNIT_NAME


def unit_content(*, command: list[str], log_path: Path,
                 workspace_root: Path) -> str:
    """Build a systemd user unit with automatic restarts and agent.log output."""
    args = " ".join(_quote_unit(part) for part in command)
    return (
        "[Unit]\n"
        "Description=Galley proofing agent (DocProof)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={args}\n"
        f"WorkingDirectory={workspace_root}\n"
        f"Environment=PATH={PATH}\n"
        f"Environment=HOME={Path.home()}\n"
        "Restart=always\n"
        "RestartSec=60\n"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n")


def _quote_unit(part: str) -> str:
    """systemd splits ExecStart on whitespace, so a path with a space in it
    has to be quoted. Nothing else needs escaping in the arguments this
    builds."""
    return f'"{part}"' if " " in part else part


def _install_systemd(*, command: list[str], workspace_root: Path,
                     run, path: Path | None) -> Path:
    target = path or unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit_content(command=command,
                                   log_path=workspace_root / LOG_NAME,
                                   workspace_root=workspace_root),
                      encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"], capture_output=True,
        text=True)
    # Lingering keeps the user service running after logout and reboot.
    # Report permission failures without undoing installation.
    linger = run(["loginctl", "enable-linger", _user()], capture_output=True,
                 text=True)
    if getattr(linger, "returncode", 0) != 0:
        log.warning("Could not enable lingering for %s — the agent will stop "
                    "when you log out. Run `sudo loginctl enable-linger %s`.",
                    _user(), _user())
    result = run(["systemctl", "--user", "enable", "--now", UNIT_NAME],
                 capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise AgentError(
            f"systemd would not start the agent"
            f"{': ' + detail if detail else '.'} The unit is written at "
            f"{target}; `systemctl --user enable --now {UNIT_NAME}` is what "
            f"failed.")
    return target


def _uninstall_systemd(*, run, path: Path | None) -> bool:
    target = path or unit_path()
    run(["systemctl", "--user", "disable", "--now", UNIT_NAME],
        capture_output=True, text=True)
    if not target.exists():
        return False
    target.unlink()
    run(["systemctl", "--user", "daemon-reload"], capture_output=True,
        text=True)
    return True


def _user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "$USER"



def service_path(platform: str | None = None) -> Path:
    """Where this machine's service definition lives."""
    return unit_path() if is_linux(platform) else plist_path()


def install(*, workspace_root: Path, env_file: Path,
            poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
            run=subprocess.run, path: Path | None = None,
            platform: str | None = None,
            wrapper_source: Path | None = None,
            wrapper_dest: Path | None = None) -> Path:
    """Install and start the platform service, then refresh the existing
    galley-run.sh wrapper.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    refresh_wrapper(source=wrapper_source, dest=wrapper_dest)
    command = program(workspace_root=workspace_root, env_file=env_file,
                      poll_interval_s=poll_interval_s)
    installer = _install_systemd if is_linux(platform) else _install_launchd
    return installer(command=command, workspace_root=workspace_root, run=run,
                     path=path)


def uninstall(*, run=subprocess.run, path: Path | None = None,
              platform: str | None = None) -> bool:
    """Stop and forget the service. Answers whether there was one."""
    remover = _uninstall_systemd if is_linux(platform) else _uninstall_launchd
    return remover(run=run, path=path)


def installed(*, path: Path | None = None,
              platform: str | None = None) -> bool:
    return (path or service_path(platform)).is_file()


def refresh_wrapper(*, source: Path | None = None,
                    dest: Path | None = None) -> Path | None:
    """Refresh an existing galley-run.sh wrapper from the repository, saving
    its previous contents as .bak.
    """
    import shutil

    src = source or (Path(__file__).resolve().parent / "practitioner"
                     / "galley-run.sh")
    target = dest or (Path.home() / "galley-bin" / "galley-run.sh")
    if not src.is_file():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == src.read_bytes():
        return target
    if target.is_file():
        shutil.copy2(target, target.with_suffix(".sh.bak"))
    shutil.copy2(src, target)
    target.chmod(0o755)
    log.info("Refreshed %s from the repo's copy.", target)
    return target


__all__ = ["AGENT_TOKEN_KEY", "APP_URL_KEY", "AWAITING_PATH", "CLAIMED",
           "DEFAULT_ENV_FILE", "DEFAULT_POLL_INTERVAL_S", "FAILED", "FINISHED",
           "HELD_FOR_CODE", "code_id",
           "LABEL", "LEDGER_NAME", "LOG_NAME", "OAUTH_KEY", "UNIT_NAME",
           "Agent", "AgentEnv", "AgentError", "AwaitingBook", "Ledger",
           "RunReport", "apply_env", "fetch_awaiting", "install", "installed",
           "poll_awaiting", "post_status", "send_alert",
           "is_linux", "parse_env", "plist_content", "plist_path", "program",
           "read_env", "refresh_wrapper", "service_path", "slug_for",
           "check_credentials", "TOKEN_FIX_HINT", "PREFLIGHT_TIMEOUT_S",
           "uninstall", "unit_content", "unit_path", "units_dir"]
