"""`docproof-watch` — set the watcher up, and run one pass.

Five commands, in the order somebody meets them: `auth` to sign in to Google,
`init` to say which folder, `once` to do a pass, `status` to see what has
happened, and `schedule` to stop having to run `once` by hand.

`once` is the whole program, really. Everything else exists so that the thing
launchd runs four times a day needs no arguments and no attention.
"""
from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

from app.jobs import JobStore
from app.lock import FolderInUse, FolderLock
from app.settings import Paths, get_api_key, set_api_key
from docproof.providers.catalog import BY_ID, MODELS

from . import auth as authlib
from . import tick as ticklib
from .drive import AuthExpired, DriveError
from .settings import (GOOGLE_KEY, WatchSettings, default_watch_home,
                       folder_id_from)
from .state import STATE_FILE, WatchState

log = logging.getLogger("docproof.app.watch.cli")

LOG_FILE = "watch.log"

# 0 clean, 2 something a person must fix before anything can run, 3 the run
# happened and some of it did not work. Same vocabulary as `docproof prep`,
# where 3 also means "it ran, and the output is not what you wanted".
OK, UNUSABLE, PARTIAL = 0, 2, 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="docproof-watch",
        description="Watch a Google Drive folder and prepare the manuscripts "
                    "that land in it for the house InDesign template.")
    ap.add_argument("--home", help="where the watcher keeps its jobs, settings "
                                   "and log (default: DOCPROOF_WATCH_HOME, or "
                                   "~/Library/Application Support/DocProof/watch)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="say what it is doing as it does it")
    sub = ap.add_subparsers(dest="cmd", required=True)

    au = sub.add_parser("auth", help="sign in to Google Drive")
    au.add_argument("--status", action="store_true",
                    help="say whether it is signed in, without signing in")
    au.add_argument("--client-id", help="the OAuth client id (it will ask if "
                                        "this is left out)")
    au.add_argument("--client-secret")

    ini = sub.add_parser("init", help="say which folder to watch")
    ini.add_argument("--folder", help="the folder's address, pasted from a "
                                      "browser, or just its id")
    ini.add_argument("--model", help="which model prepares the manuscripts")
    ini.add_argument("--output", choices=["indesign", "tracked", "both"],
                     help="which file(s) to put back in the folder")

    on = sub.add_parser("once", help="do one pass over the folder now")
    on.add_argument("--dry-run", action="store_true",
                    help="say what a pass would do, without downloading, "
                         "preparing or uploading anything")
    on.add_argument("--mock-tags", action="store_true",
                    help="do the whole round trip with no model call, to "
                         "rehearse the Drive side without spending anything")

    sub.add_parser("status", help="what the watcher has done lately")

    args = ap.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else default_watch_home()
    _logging(home, verbose=args.verbose)
    return {"auth": cmd_auth, "init": cmd_init, "once": cmd_once,
            "status": cmd_status}[args.cmd](args, home)


def _logging(home: Path, *, verbose: bool) -> None:
    """Terminal and file both. The file is what a scheduled run leaves behind,
    and the only place anybody can look afterwards to see why a morning was
    quiet."""
    root = logging.getLogger("docproof")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Cleared first so a second call in one process — which is what a test
    # does — does not print everything twice.
    root.handlers.clear()

    # The terminal gets what went wrong; the commands print what happened
    # themselves. "This DocProof owns /Users/…" is true, useful in a log file
    # and noise in front of somebody who just typed a command.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    root.addHandler(console)

    try:
        home.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(home / LOG_FILE, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    except OSError as e:                  # noqa: BLE001 - a log is not the job
        print(f"note: could not write a log in {home} ({e})", file=sys.stderr)


# --- signing in ---------------------------------------------------------------

def cmd_auth(args, home: Path) -> int:
    ws = WatchSettings.load(home)

    if args.status:
        state = authlib.token_source(get_api_key, bool(ws.client_id))
        if not state["client"]:
            print("No Google OAuth client yet. docs/watch.md walks through "
                  "making one in the Google Cloud console.")
        if state["configured"]:
            where = ("the environment" if state["source"] == "environment"
                     else "the Keychain")
            print(f"Signed in to Google — the sign-in came from {where}.")
        else:
            print("Not signed in to Google. Run `docproof-watch auth`.")
        return OK if state["configured"] else UNUSABLE

    client_id = args.client_id or ws.client_id or _ask(
        "The OAuth client id from the Google Cloud console")
    client_secret = args.client_secret or ws.client_secret or _ask(
        "The client secret for it")
    if not client_id or not client_secret:
        print("error: signing in needs an OAuth client. docs/watch.md walks "
              "through making one.", file=sys.stderr)
        return UNUSABLE

    print("\nA browser is about to open so Google can ask whether DocProof "
          "may read this Drive.\nNothing is typed into DocProof — the "
          "password stays with Google.\n")
    try:
        token = authlib.run_flow(client_id, client_secret,
                                 open_browser=_open_browser)
    except DriveError as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE

    set_api_key(GOOGLE_KEY, token)
    ws.client_id, ws.client_secret = client_id, client_secret
    ws.save(home)
    print("\nSigned in. The sign-in is in your Keychain, not in a file.")
    if not ws.folder_id:
        print("Next: `docproof-watch init --folder <the folder's address>`.")
    return OK


def _open_browser(url: str) -> bool:
    """Print it as well as open it. A Mac somebody is ssh'd into has no
    browser to open, and that should not be the end of the road."""
    print(f"If the browser does not open, go to:\n\n  {url}\n")
    return webbrowser.open(url)


def _ask(prompt: str) -> str:
    try:
        return input(f"{prompt}: ").strip()
    except EOFError:
        return ""


# --- what to watch ------------------------------------------------------------

def cmd_init(args, home: Path) -> int:
    ws = WatchSettings.load(home)

    if args.folder:
        try:
            ws.folder_id = folder_id_from(args.folder)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return UNUSABLE
    if args.model:
        if args.model not in BY_ID:
            print(f"error: {args.model} is not a model DocProof knows. "
                  f"Choose from: {', '.join(m.id for m in MODELS)}",
                  file=sys.stderr)
            return UNUSABLE
        ws.model = args.model
    if args.output:
        ws.prep_output = args.output
    ws.save(home)

    print(f"Watching folder {ws.folder_id or '— not set yet'}")
    print(f"Preparing with {ws.model}, handing back: {ws.prep_output}")
    print(f"Keeping its things in {home}")
    missing = _missing(ws)
    if missing:
        print(f"\nStill needed: {missing}")
        return UNUSABLE
    print("\nReady. Try `docproof-watch once --dry-run` to see what it would "
          "do.")
    return OK


def _missing(ws: WatchSettings) -> str:
    if not ws.folder_id:
        return "a folder to watch — `docproof-watch init --folder <address>`"
    if not ws.client_id or not ws.client_secret or not get_api_key(GOOGLE_KEY):
        return "a Google sign-in — `docproof-watch auth`"
    return ""


# --- one pass -----------------------------------------------------------------

def cmd_once(args, home: Path) -> int:
    ws = WatchSettings.load(home)
    try:
        # Read-only passes claim nothing: they start no runner and write no
        # job, so there is nothing for a second copy to collide with.
        if args.dry_run:
            return _report(ticklib.tick(home, ws, dry_run=True))
        with FolderLock(home):
            return _report(ticklib.tick(home, ws, mock=args.mock_tags))
    except FolderInUse as e:
        # Normal rather than exceptional: a long book can outlast the gap
        # between two scheduled runs, and the right answer is to let the first
        # one finish. Not an error, so launchd is not told one happened.
        print(f"A previous run is still working on this folder, so this one "
              f"stopped. ({e})")
        return OK
    except AuthExpired as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE
    except (ticklib.NotConfigured, DriveError) as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE


def _report(report: ticklib.TickReport) -> int:
    if report.dry_run:
        print(f"{report.listed} file(s) in the folder:\n")
        for name, stage in report.plan:
            print(f"  {_PLAIN.get(stage, stage):<22} {name}")
        print(f"\nA real run would prepare {report.new} manuscript(s). "
              f"Nothing was downloaded, prepared or uploaded.")
        return OK

    if report.prepped:
        print(f"Prepared {len(report.prepped)}: "
              f"{', '.join(report.prepped)}")
        for name in report.uploaded:
            print(f"  → {name}")
    if report.deferred:
        print(f"{report.deferred} more manuscript(s) are waiting; the next run "
              f"will take them.")
    for name, reason in report.failed:
        print(f"Could not prepare {name}: {reason}", file=sys.stderr)
    if not report.prepped and not report.failed:
        print(f"Nothing new in the folder ({report.listed} file(s) looked at).")
    return OK if report.ok else PARTIAL


_PLAIN = {"new": "to prepare", "done": "already prepared",
          "failed": "needs attention", "output": "DocProof wrote this",
          "skip": "not a manuscript"}


# --- what it has been doing ---------------------------------------------------

def cmd_status(args, home: Path) -> int:
    ws = WatchSettings.load(home)
    print(f"Folder:  {ws.folder_id or '— not set yet'}")
    print(f"Model:   {ws.model}")
    print(f"Home:    {home}")
    signed = authlib.token_source(get_api_key, bool(ws.client_id))
    print(f"Signed in: {'yes' if signed['configured'] else 'no'}")
    missing = _missing(ws)
    if missing:
        print(f"\nStill needed: {missing}")

    state = WatchState.load(home / STATE_FILE)
    if not state.files:
        print("\nNothing has been prepared yet.")
        return OK

    store = JobStore(Paths(home))
    jobs = {job.id: job for job in store.all()}
    print(f"\n{len(state.files)} manuscript(s) seen:\n")
    for rec in sorted(state.files.values(), key=lambda r: r.updated_at,
                      reverse=True):
        job = jobs.get(rec.job_id)
        cost = f"  ${job.cost:.2f}" if job and job.cost else ""
        said = rec.marked or (job.state if job else "in progress")
        print(f"  {said:<12} {rec.name}{cost}")
        for name in rec.uploaded:
            print(f"               → {name}")
    return OK
