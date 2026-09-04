"""The practitioner-side proofing agent — the piece that makes the loop
unattended.

DocWatch runs on Fly and cannot run Galley: the practitioner's brain is a
Claude Max subscription, which lives on one machine of the owner's (a Mac
today, a rented Linux box later). So the proofing stage in
`external` mode stops half way — DocWatch finds `<surname> - Book 1.docx` at
"Ready for Proofing", marks the manuscript `awaiting`, emails the owner, and
waits for a person to download the book and type `docproof galley drive`.

This module is that person.

    while True:
        ask the Fly app which books are awaiting a practitioner
        for the first one this machine has not already claimed:
            download the Book 1 with the watcher's own Google sign-in
            run galley.driver over it, --approve auto, $10, mechanical only
            the driver's hand-off uploads the Book 2 set to the author folder
        sleep --poll-interval

DocWatch's next tick reads the `outcome.json` that lands beside the book and
moves HubSpot on. Nothing else has to happen.

Five things this is careful about, all of them because it runs unwatched:

* **A book is never read twice.** A galley run over a whole novel is the most
  expensive mistake this program can make, so a local ledger
  (`~/galley-workspaces/.agent-state.json`) records every file id claimed,
  finished or failed, and a claim is written BEFORE the work starts. A crash
  mid-run resumes the same workspace with `--from`, it does not start again.
* **A book always comes back with an answer.** A download that fails, a driver
  that crashes, a run that stops — each writes a `needs_human` outcome.json
  naming what went wrong and uploads it (with the decision log) to the author's
  folder, so DocWatch's next tick moves the book on instead of leaving it at
  "Ready for Proofing" forever.
* **It never loops on the same book.** A failure is terminal in the ledger. The
  next poll moves to the next book; the failed one waits for a person.
* **Keys stay where they belong.** The brain's OAuth token and the Fly
  credential are read from `~/.galley/agent.env`, which must not be readable by
  anyone but its owner, and never from the shell — a service manager starts a
  job with almost no environment, and a token in a shell profile is a token in
  every process. The sifters' API keys stay in `~/.docproof-eval.env`, reached only
  through the `~/galley-bin/docproof` wrapper, exactly as the driver arranges.
* **One book at a time.** Two galley runs on one machine would fight over the
  subscription's rate limits and the wrapper's venv.
* **Nothing here is macOS-only.** The poll, the download, the driver and the
  ledger are plain Python and plain HTTPS. Only "keep this running" differs by
  machine, and only that is behind a platform switch: a launchd LaunchAgent on
  macOS, a systemd user unit on Linux.
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

#: Where the poller's credentials live. Mode 600 or the agent refuses to start.
DEFAULT_ENV_FILE = "~/.galley/agent.env"
#: The ledger of what this machine has claimed, finished and failed.
LEDGER_NAME = ".agent-state.json"
#: Where the service writes everything the agent says, on either platform.
LOG_NAME = "agent.log"
#: The service's name: launchd's Label, and the stem of the systemd unit.
LABEL = "com.atmosphere.galley-agent"
#: Downloads land here, one directory per Drive file id, so two books with the
#: same name cannot overwrite each other.
DOWNLOAD_DIR = ".agent-downloads"

DEFAULT_POLL_INTERVAL_S = 300.0
#: What the server calls the read-only route this poller lives on.
AWAITING_PATH = "/api/watch/awaiting"
#: A service manager starts a job with almost no environment; the driver needs
#: to find `claude`, the wrapper and the venv. Homebrew's prefixes are harmless
#: on Linux (a missing directory on PATH is ignored), so one list serves both.
PATH = ("/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
        + str(Path.home() / ".local" / "bin"))

#: The keys `~/.galley/agent.env` is read for. Everything else in the file is
#: passed through to the driver's environment untouched, so a machine that
#: needs one more variable (GOOGLE_REFRESH_TOKEN on Linux, where there is no
#: Keychain for `get_api_key` to fall back to) does not need a code change.
OAUTH_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
APP_URL_KEY = "GALLEY_APP_URL"
AGENT_TOKEN_KEY = "GALLEY_AGENT_TOKEN"


class AgentError(RuntimeError):
    """A setup problem the agent refuses to start on. The message is the fix."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- the environment file -----------------------------------------------------

@dataclass(frozen=True)
class AgentEnv:
    """What `~/.galley/agent.env` says.

    `values` is the whole file, so anything the owner adds rides through to the
    driver's environment; the three named fields are what this module itself
    needs."""

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
    """A shell-ish `KEY=value` file, without running a shell.

    `export` is tolerated because everyone writes it; quotes are stripped
    because everyone uses them; a `#` line is a comment. Nothing is executed —
    the file holds secrets, and sourcing it would run whatever else is in it."""
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
    """Read the credentials, refusing a file anyone else can read.

    The permission check is not decoration. This file holds a Claude Max
    subscription token and the key to a read-only route on the production
    server; on a shared Mac a group-readable copy is a copy in somebody else's
    hands. Refused with the one command that fixes it.

    The same check on Linux, for the same reason and with the same fix."""
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
    """Put the credentials file into this process's own environment.

    Two things need it there rather than only in the driver's child
    environment. The obvious one is `CLAUDE_CODE_OAUTH_TOKEN`. The other is the
    Google refresh token: `app.settings.get_api_key` reads the environment
    FIRST and falls back to `keyring`, and keyring has no backend on a headless
    Linux box — so on Linux `GOOGLE_REFRESH_TOKEN` belongs in
    `~/.galley/agent.env` and this is what makes `docproof-watch`'s own Drive
    sign-in work there. On macOS the Keychain already answers and the file
    simply need not carry it.

    The file wins over the ambient environment on purpose: it is the thing the
    owner edited, and a stale token in a shell profile should not outrank it."""
    target = environ if environ is not None else os.environ
    for key, value in env.values.items():
        if value:
            target[key] = value


# --- what the server says -----------------------------------------------------

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
    """Ask the app which books are awaiting a practitioner.

    Every failure is a WARNING and an empty list, never an exception: the
    poller runs forever, and a Fly deploy or a dropped Wi-Fi connection must
    cost one poll, not the agent."""
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


# --- the ledger ---------------------------------------------------------------

CLAIMED, FINISHED, FAILED = "claimed", "finished", "failed"


@dataclass
class Ledger:
    """What this Mac has done with each Drive file id.

    The durable answer to "have I already read this book?" — written before the
    work starts, so a crash resumes rather than repeats. Drive's own markers
    and DocWatch's state are the other half of that answer, but they are a
    network call away and this one is a file read."""

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


# --- naming -------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slug_for(name: str, author_last: str = "") -> str:
    """The workspace name for a book: `"Test - Book 1.docx"` -> `test-book-1`.

    Built from the file's own stem rather than a template, so the stage the
    workspace is named for is the stage the file actually carries — a
    `"Test - Book One.docx"` becomes `test-book-one` and nobody has to wonder
    which of two workspaces held which spelling. A file whose stem slugs to
    nothing falls back to the author's surname, and then to the Drive id's
    shape, because a workspace must always have a name."""
    stem = Path(name).stem
    slug = _SLUG_STRIP.sub("-", stem.lower()).strip("-")
    if slug:
        return slug
    fallback = _SLUG_STRIP.sub("-", (author_last or "").lower()).strip("-")
    return f"{fallback}-book" if fallback else "untitled-book"


# --- the agent ----------------------------------------------------------------

@dataclass
class RunReport:
    """What one poll did, for the log and for `--once`'s exit code."""

    looked_at: int = 0
    claimed: str = ""
    outcome: str = ""
    reason: str = ""
    skipped: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"looked_at": self.looked_at, "claimed": self.claimed,
                "outcome": self.outcome, "reason": self.reason,
                "skipped": list(self.skipped)}


@dataclass
class Agent:
    """The poller. Every side channel is injectable, so the whole loop is
    testable without Fly, without Drive, and without a headless session."""

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

    # -- paths --

    @property
    def root(self) -> Path:
        return Path(str(self.workspace_root)).expanduser()

    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    def ledger(self) -> Ledger:
        return Ledger.load(self.ledger_path)

    # -- one poll --

    def poll_once(self) -> RunReport:
        """Look once, and run at most one book."""
        report = RunReport()
        books = fetch_awaiting(self.env, opener=self.opener)
        report.looked_at = len(books)
        ledger = self.ledger()

        for book in books:
            state = ledger.state(book.file_id)
            if state in (FINISHED, FAILED):
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
                # The poller outlives every failure inside it. A book that
                # blew up is recorded as failed by `run_book`; anything that
                # escapes to here is a bug, and sleeping through it is still
                # better than a launchd job that dies at 3am.
                log.exception("The poll failed; trying again next interval.")
            self.sleep(self.poll_interval_s)

    # -- one book --

    def run_book(self, book: AwaitingBook, ledger: Ledger, report: RunReport,
                 *, resume: bool = False) -> None:
        """Download it, run the driver over it, and record what happened.

        The claim is written FIRST — before the download, before a single
        model call — because the failure this guards against is running a whole
        novel twice, and a claim written afterwards would be written after the
        crash that loses it."""
        slug = slug_for(book.name, book.author_last)
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
        ledger.record(book.file_id, FINISHED if outcome == "done" else FAILED,
                      name=book.name, slug=slug, folder_id=folder,
                      outcome=outcome, reason=reason[:400],
                      uploaded=list(getattr(result, "uploaded", []) or []))
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
        # Only the id and the name are known from the awaiting list; the mime
        # type decides download-vs-export, and a Book 1 that is a native Google
        # Doc is exported. `fetch` handles both, plus .doc/.odt conversion.
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
            # A claimed-but-unfinished book is a crashed run: pick it up where
            # the workspace's own state machine says it got to, rather than
            # paying for the ladder twice.
            start = self.resume_phase(slug)
            if start:
                kwargs["start_phase"] = start
                self.log(f"Resuming {slug} from the {start} phase.")
        runner = self.run_driver or _run_driver
        return runner(book=local, slug=slug, workspace_root=self.root,
                      drive_folder_id=folder_id,
                      env=self.driver_env(), upload=self.upload, **kwargs)

    def resume_phase(self, slug: str) -> str:
        """The phase to restart a crashed run at, from its own state machine.

        One phase back from where the ledger says it got to would be a guess;
        `state.json` is the record, and the driver's own phase order says which
        phase comes after the last state reached."""
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
        """The environment the driver's sessions inherit.

        The process environment plus the credentials file, so a launchd job —
        which starts with almost none of one — still has a PATH, a HOME and the
        subscription token. The driver strips the API keys from it and puts the
        `~/galley-bin` wrapper first, which is what keeps the brain on the
        subscription and the sifters on the keys."""
        env = dict(os.environ)
        env.setdefault("PATH", PATH)
        env.update(self.env.values)
        return env

    def give_up(self, book: AwaitingBook, ledger: Ledger, report: RunReport,
                slug: str, folder_id: str, reason: str) -> None:
        """Write a `needs_human` verdict for a book that never got one, and put
        it in the author's folder.

        The whole point: a book DocWatch marked `awaiting` sits at "Ready for
        Proofing" until an outcome.json lands beside it. A download that failed
        or a driver that crashed would otherwise strand it there silently, and
        the owner would find out weeks later. So the failure is written as a
        verdict, uploaded, and the ledger marks the book done-with — never
        retried, because a loop that retries a crash is a loop that crashes
        forever."""
        report.outcome, report.reason = "needs_human", reason
        uploaded: list[str] = []
        try:
            files = self.write_failure(slug, book.name, reason)
            if folder_id and files:
                uploader = self.upload or _default_upload
                uploaded = uploader(files, folder_id)
        except Exception as e:                              # noqa: BLE001
            log.exception("Could not deliver the failure verdict for %s",
                          book.name)
            self.log(f"{book.name} failed and its verdict could not be "
                     f"delivered ({e}) — DocWatch will keep waiting on this "
                     f"book until somebody looks at it.")
        ledger.record(book.file_id, FAILED, name=book.name, slug=slug,
                      folder_id=folder_id, outcome="needs_human",
                      reason=reason[:400], uploaded=uploaded)
        self.log(f"{book.name}: needs_human — {reason[:200]}")

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
            # Not even a partial hand-off could be built. The verdict itself is
            # the one file that matters, so it goes on its own under the name
            # DocWatch looks for.
            out.mkdir(parents=True, exist_ok=True)
            import shutil
            dest = out / f"{handoff_base(source_name)} - outcome.json"
            shutil.copy2(runs / "outcome.json", dest)
            return [dest]

    # -- what the ledger says --

    def status(self) -> dict[str, Any]:
        ledger = self.ledger()
        return {"workspace_root": str(self.root),
                "ledger": str(self.ledger_path),
                "app": self.env.awaiting_url,
                "books": ledger.books,
                "pending": ledger.pending()}


def _state_index(state: str) -> int:
    from galley.state_machine import RUN_STATES
    return RUN_STATES.index(state) if state in RUN_STATES else -1


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_BY_SUFFIX = {".docx": _DOCX, ".doc": "application/msword",
                   ".odt": "application/vnd.oasis.opendocument.text"}


def _mime_for(name: str) -> str:
    """What a Drive file of this name most likely is.

    The awaiting list carries the name, not the mime type. A name with no
    extension is a native Google Doc — that is what a Doc looks like in Drive —
    and `fetch` exports those; anything else is downloaded as-is."""
    suffix = Path(name).suffix.lower()
    if not suffix:
        from app.watch.drive import GOOGLE_DOC_MIME
        return GOOGLE_DOC_MIME
    return _MIME_BY_SUFFIX.get(suffix, _DOCX)


def _run_driver(**kwargs: Any) -> Any:
    """The real driver, in this process — never a subprocess shell-out.

    In-process so the agent sees the `DriveResult` itself: the outcome, the
    reason and what was uploaded, rather than an exit code it would have to
    interpret. The driver spawns its own headless sessions; that is the
    subprocess boundary, and it is already the right one."""
    from galley.driver import Driver

    upload = kwargs.pop("upload", None)
    driver = Driver(approve="auto", mechanical_only=True, **kwargs)
    if upload is not None:
        driver.upload = upload
    return driver.run()


def _default_upload(files: list[Path], folder_id: str) -> list[str]:
    from galley.driver import _default_upload as upload
    return upload(files, folder_id)


# --- running it as a service --------------------------------------------------
#
# The agent itself is platform-neutral: it polls an HTTPS endpoint, downloads a
# file, and runs the driver. Only "keep this running" differs by machine, so
# only that is behind a platform switch — a launchd LaunchAgent on macOS, a
# systemd user unit on Linux, both with the same env file, the same log, and
# the same command line. The owner's Mac holds the subscription today; a rented
# Linux box is the same agent with a different unit file.

def is_linux(platform: str | None = None) -> bool:
    return (platform or sys.platform).startswith("linux")


def executable() -> str:
    """The `docproof` to schedule, by absolute path.

    `sys.argv[0]` first, because the copy running right now is the copy the
    owner means: a machine with two virtualenvs would otherwise get whichever
    one is earlier in a PATH the service manager does not share."""
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
    """Everything is spelled out — a service manager passes almost no
    environment, and a poller that quietly used a different workspace root or a
    different credentials file would be a puzzle nobody enjoys."""
    return [executable(), "galley", "agent",
            "--workspace-root", str(workspace_root),
            "--env-file", str(env_file),
            "--poll-interval", str(int(poll_interval_s))]


# -- macOS: a LaunchAgent --

def agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def plist_content(*, command: list[str], log_path: Path,
                  workspace_root: Path) -> bytes:
    """The launch agent.

    `KeepAlive` rather than a calendar interval: this is a poller, not a
    scheduled pass — it should be running whenever the machine is, and launchd
    should start it again if it dies. `RunAtLoad` is true for the same reason,
    and unlike the watcher's schedule it costs nothing to start: the first poll
    only asks the app a question."""
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        # A crash loop should back off rather than spin: launchd will not
        # restart the job more than once every 60 seconds.
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
    # Unloaded first so a changed agent replaces the old one rather than being
    # refused for already existing. It fails when nothing is loaded, which is
    # the ordinary case the first time and not worth reporting.
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


# -- Linux: a systemd user unit --

#: The unit's file name. `galley-agent`, not the reverse-DNS label: systemd
#: units are named the way a person types them into `systemctl --user`.
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
    """The systemd user unit.

    `Restart=always` is systemd's `KeepAlive`; `RestartSec` is its
    `ThrottleInterval`. Output goes to the same `agent.log` the LaunchAgent
    writes rather than to the journal, so "read the log" is one answer on both
    machines. `default.target` is the user-session equivalent of RunAtLoad —
    with lingering enabled (see `install`) that means "whenever the box is
    up", which is the point of renting one."""
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
    # Lingering is what makes a user unit survive logout and come back after a
    # reboot — without it a rented box runs the agent only while somebody is
    # signed in over SSH. It needs root, so a refusal is reported rather than
    # raised: the unit is installed and works for this session either way.
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


# -- the switch --

def service_path(platform: str | None = None) -> Path:
    """Where this machine's service definition lives."""
    return unit_path() if is_linux(platform) else plist_path()


def install(*, workspace_root: Path, env_file: Path,
            poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
            run=subprocess.run, path: Path | None = None,
            platform: str | None = None,
            wrapper_source: Path | None = None,
            wrapper_dest: Path | None = None) -> Path:
    """Write this machine's service definition and start it.

    A LaunchAgent on macOS, a systemd user unit on Linux — same command line,
    same env file, same log. Also refreshes the `~/galley-bin/galley-run.sh`
    wrapper from the repo's copy when one is there to refresh: the phase
    prompts live in the repo now, and a machine still carrying the pre-driver
    script would run a stale loop the moment somebody typed a phase by hand."""
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
    """Copy the repo's `galley-run.sh` over the one in `~/galley-bin`.

    The install verb does this so the owner never has to; nothing else in
    DocProof writes outside the repo. The old copy is kept beside it as
    `.bak` — it is somebody's working script until it is not."""
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
