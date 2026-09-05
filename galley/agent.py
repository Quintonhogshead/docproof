"""Poll DocWatch for manuscripts, run the Galley driver, and upload results.

Track claims and pending deliveries in a local ledger. Service installers
support launchd on macOS and systemd on Linux.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("docproof.galley.agent")

# Credentials file; group/other permissions are forbidden.
DEFAULT_ENV_FILE = "~/.galley/agent.env"
#: The ledger of what this machine has claimed, finished and failed.
LEDGER_NAME = ".agent-state.json"
#: Where the service writes everything the agent says, on either platform.
LOG_NAME = "agent.log"
# launchd service label.
LABEL = "com.atmosphere.galley-agent"
# Store downloads by Drive id to separate identically named books.
DOWNLOAD_DIR = ".agent-downloads"

DEFAULT_POLL_INTERVAL_S = 300.0
#: What the server calls the read-only route this poller lives on.
AWAITING_PATH = "/api/watch/awaiting"
# Service PATH defaults include the CLI and common Homebrew locations.
PATH = ("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
        + str(Path.home() / ".local" / "bin"))

# Named credential keys; additional file values also pass into the driver
# environment.
OAUTH_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
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


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=value lines without shell evaluation; allow export, paired
    quotes, and comment lines.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[key] = raw
    return out


def read_env(path: str | Path = DEFAULT_ENV_FILE, *,
             stat_fn: Callable[[Path], Any] | None = None) -> AgentEnv:
    """Load credentials; reject group/other permissions and missing required
    values.
    """
    target = Path(str(path)).expanduser()
    if not target.is_file():
        raise AgentError(
            f"No agent credentials at {target}. Create it with:\n"
            f"    mkdir -p {target.parent} && touch {target} && "
            f"chmod 600 {target}\n"
            f"then put {OAUTH_KEY} (from `claude setup-token`), "
            f"{APP_URL_KEY} and {AGENT_TOKEN_KEY} in it.")
    info = (stat_fn or (lambda p: p.stat()))(target)
    mode = stat.S_IMODE(info.st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AgentError(
            f"{target} is readable by other accounts (mode {mode:04o}) and it "
            f"holds a subscription token. Fix it with:\n"
            f"    chmod 600 {target}")
    values = parse_env(target.read_text(encoding="utf-8"))
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

    @classmethod
    def from_json(cls, raw: dict) -> "AwaitingBook":
        return cls(file_id=str(raw.get("file_id", "")),
                   name=str(raw.get("name", "")),
                   folder_id=str(raw.get("folder_id")
                                 or raw.get("subfolder_id") or ""),
                   author_last=str(raw.get("author_last", "")))


def _open_url(request: urllib.request.Request, timeout: int = 30):
    """The one place this module touches the network, passed in by every caller
    so no test ever reaches Fly."""
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_awaiting(env: AgentEnv, *, opener=_open_url) -> list[AwaitingBook]:
    """Fetch awaiting manuscripts; log request failures and return an empty
    list.
    """
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
        log.warning("The app refused the awaiting list (HTTP %s)%s",
                    e.code, f": {detail}" if detail else "")
        return []
    except Exception as e:                                  # noqa: BLE001
        log.warning("Could not reach %s (%s); trying again next poll.",
                    env.awaiting_url, e)
        return []
    if not isinstance(payload, dict):
        log.warning("The app answered something that is not an awaiting list.")
        return []
    books = [AwaitingBook.from_json(row)
             for row in (payload.get("books") or []) if isinstance(row, dict)]
    return [b for b in books if b.file_id and b.name]



CLAIMED, FINISHED, FAILED = "claimed", "finished", "failed"
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

    def to_json(self) -> dict[str, Any]:
        return {"looked_at": self.looked_at, "claimed": self.claimed,
                "outcome": self.outcome, "reason": self.reason,
                "skipped": list(self.skipped)}


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
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = log.info


    @property
    def root(self) -> Path:
        return Path(str(self.workspace_root)).expanduser()

    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def ledger(self) -> Ledger:
        return Ledger.load(self.ledger_path)


    def poll_once(self) -> RunReport:
        """Look once, and run at most one book."""
        report = RunReport()
        books = fetch_awaiting(self.env, opener=self.opener)
        report.looked_at = len(books)
        ledger = self.ledger()

        # Retry pending delivery before starting another book.
        self.retry_deliveries(ledger, report)

        for book in books:
            state = ledger.state(book.file_id)
            if state in (FINISHED, FAILED, PENDING_DELIVERY):
                report.skipped.append(f"{book.name} ({state})")
                continue
            resume = state == CLAIMED
            self.run_book(book, ledger, report, resume=resume)
            return report                     # one book at a time, on purpose
        if books:
            self.log(f"{len(books)} book(s) awaiting; all already handled here.")
        return report

    def run_forever(self) -> None:
        self.log(f"Galley agent: polling {self.env.awaiting_url} every "
                 f"{self.poll_interval_s / 60:.0f} min.")
        while True:
            try:
                self.poll_once()
            except Exception:                               # noqa: BLE001
                # Keep polling after unexpected failures.
                log.exception("The poll failed; trying again next interval.")
            self.sleep(self.poll_interval_s)


    def run_book(self, book: AwaitingBook, ledger: Ledger, report: RunReport,
                 *, resume: bool = False) -> None:
        """Record the claim before downloading, then run the driver and record
        its outcome.
        """
        slug = slug_for(book.name, book.author_last, book.file_id)
        report.claimed = book.name
        folder = self.drive_folder_override or book.folder_id
        ledger.record(book.file_id, CLAIMED, name=book.name, slug=slug,
                      folder_id=folder)
        self.log(f"{'Resuming' if resume else 'Claiming'} {book.name} "
                 f"(workspace {slug}).")

        try:
            local = self.fetch_book(book)
        except Exception as e:                              # noqa: BLE001
            log.exception("Could not download %s", book.name)
            self.give_up(book, ledger, report, slug, folder,
                         f"DocProof could not download {book.name} from Drive "
                         f"({e}), so the proofread never started. The book "
                         f"needs a human proofreader, or a fixed Google "
                         f"sign-in on the practitioner's Mac.")
            return

        self._file_id = book.file_id
        try:
            result = self.drive_book(local, slug, folder, resume=resume)
        except Exception as e:                              # noqa: BLE001
            log.exception("The proofread of %s crashed", book.name)
            self.give_up(book, ledger, report, slug, folder,
                         f"The proofreading run over {book.name} crashed "
                         f"({e}). Nothing was delivered; the book needs a "
                         f"human proofreader.")
            return

        outcome = getattr(result, "outcome", "needs_human")
        reason = getattr(result, "reason", "")
        report.outcome, report.reason = outcome, reason
        uploaded = list(getattr(result, "uploaded", []) or [])
        handoff = [Path(p) for p in (getattr(result, "handoff", []) or [])]
        if folder and handoff and not uploaded:
            # Queue a failed handoff upload before marking the book
            # finished.
            self.owe_delivery(book, ledger, slug, folder, outcome, reason,
                              handoff, why="the driver's upload failed")
            return
        ledger.record(book.file_id, FINISHED if outcome == "done" else FAILED,
                      name=book.name, slug=slug, folder_id=folder,
                      outcome=outcome, reason=reason[:400],
                      uploaded=uploaded)
        self.log(f"{book.name}: {outcome} — {reason[:200]}")

    def fetch_book(self, book: AwaitingBook) -> Path:
        """The Book 1, on this Mac, as a .docx."""
        dest = self.root / DOWNLOAD_DIR / book.file_id
        if self.download is not None:
            return self.download(book, dest)
        from app.watch.drive import DriveFile
        from app.watch.proof import fetch
        from galley.driver import drive_token

        token = drive_token()
        # Infer download versus native-Doc export from the name; fetch also
        # converts .doc/.odt.
        handle = DriveFile(id=book.file_id, name=book.name,
                           mime_type=_mime_for(book.name))
        return fetch(token, handle, dest)

    def drive_book(self, local: Path, slug: str, folder_id: str, *,
                   resume: bool) -> Any:
        """Run the practitioner loop over one manuscript, in this process."""
        kwargs: dict[str, Any] = {}
        if self.budget_usd is not None:
            kwargs["budget_usd"] = self.budget_usd
        if resume:
            # Resume claimed work from its recorded state.
            start = self.resume_phase(slug)
            if start:
                kwargs["start_phase"] = start
                self.log(f"Resuming {slug} from the {start} phase.")
        runner = self.run_driver or _run_driver
        return runner(book=local, slug=slug, workspace_root=self.root,
                      drive_folder_id=folder_id, source_id=self._file_id,
                      env=self.driver_env(), upload=self.upload, **kwargs)

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
        return MECHANICAL_PHASES[after] if after < len(MECHANICAL_PHASES) else ""

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
                continue
            uploaded_names = dict(entry.get("uploaded_names") or {})
            if self._upload_missing(files, folder, uploaded_names):
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

    def write_failure(self, slug: str, source_name: str,
                      reason: str) -> list[Path]:
        """The hand-off for a book that never ran: a verdict, and the decision
        log if there is anything to log."""
        from galley.driver import build_handoff, handoff_base
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
            return build_handoff(ws, source_name, out,
                                 outcome_sources=[runs / "outcome.json"],
                                 partial=True)
        except Exception:                                   # noqa: BLE001
            # If a partial handoff cannot be built, deliver the outcome
            # alone.
            out.mkdir(parents=True, exist_ok=True)
            import shutil
            dest = out / f"{handoff_base(source_name)} - outcome.json"
            shutil.copy2(runs / "outcome.json", dest)
            return [dest]


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
    driver = Driver(approve="auto", mechanical_only=True, **kwargs)
    if upload is not None:
        driver.upload = upload
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
           "LABEL", "LEDGER_NAME", "LOG_NAME", "OAUTH_KEY", "UNIT_NAME",
           "Agent", "AgentEnv", "AgentError", "AwaitingBook", "Ledger",
           "RunReport", "apply_env", "fetch_awaiting", "install", "installed",
           "is_linux", "parse_env", "plist_content", "plist_path", "program",
           "read_env", "refresh_wrapper", "service_path", "slug_for",
           "uninstall", "unit_content", "unit_path", "units_dir"]
