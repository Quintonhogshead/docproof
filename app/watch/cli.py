"""`docproof-watch` — set the watcher up, and run one pass.

Five commands, in the order somebody meets them: `auth` to sign in to Google,
`init` to say which folder, `once` to do a pass, `status` to see what has
happened, and `schedule` to stop having to run `once` by hand.

`once` is the whole program, really. Everything else exists so that the thing
launchd runs twice a day needs no arguments and no attention.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
import webbrowser
from pathlib import Path

from app.lock import FolderInUse, FolderLock
from app.settings import get_api_key, set_api_key
from docproof.providers.catalog import BY_ID, MODELS

from . import auth as authlib
from . import daily as dailylib
from . import schedule as schedulelib
from . import status as statuslib
from . import tick as ticklib
from .drive import AuthExpired, DriveError
from .settings import (HUBSPOT_KEY, WatchSettings, default_watch_home,
                       folder_id_from)

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
    ini.add_argument("--output",
                     choices=["book", "indesign", "tracked", "both", "all"],
                     help="which file(s) to put back in the folder")
    # The in-app clock — the one that runs while DocProof (or the server) is up,
    # as opposed to the launchd `schedule` command that runs while a Mac is
    # closed. On the always-on server this is the whole schedule.
    ini.add_argument("--auto", dest="auto_ticks", action="store_true",
                     default=None,
                     help="look on a schedule while DocProof is running")
    ini.add_argument("--no-auto", dest="auto_ticks", action="store_false",
                     default=None, help="stop looking on that schedule")
    ini.add_argument("--at-times",
                     help="fixed times of day for that clock, as HH:MM,HH:MM "
                          "(e.g. 09:00,17:00); empty (--at-times '') clears "
                          "them and goes back to --every")
    ini.add_argument("--timezone",
                     help="the zone --at-times is read in, an IANA name like "
                          "America/New_York; blank means this machine's own")
    ini.add_argument("--every", type=int,
                     help="how often that clock looks when no fixed times are "
                          "set, in minutes (5–1440)")
    ini.add_argument("--notify-email",
                     help="email this address when a pass needs a person "
                          "(sent via Gmail as the signed-in Google account)")
    ini.add_argument("--notify-on-complete", dest="notify_on_complete",
                     action="store_true", default=None,
                     help="also email a full log on every finished job — a "
                          "watched book, a dropped review, prep or promo, alike")
    ini.add_argument("--no-notify-on-complete", dest="notify_on_complete",
                     action="store_false", default=None,
                     help="stop emailing on every finished job")
    ini.add_argument("--enable-subfolders", action="store_true",
                     help="treat the watched folder as a parent Author Folder "
                          "and route each book into its author's 'First Last' "
                          "subfolder (needs HubSpot on)")
    ini.add_argument("--disable-subfolders", action="store_true",
                     help="read the watched folder flat again")
    ini.add_argument("--require-source-label", dest="require_source_label",
                     action="store_true", default=None,
                     help="prepare only the manuscript named "
                          "'<surname> - Book Original', leaving drafts and "
                          "developmental copies alone; works flat or in "
                          "per-author subfolders")
    ini.add_argument("--no-require-source-label", dest="require_source_label",
                     action="store_false", default=None,
                     help="prepare any new manuscript in the author's folder "
                          "(the default)")
    ini.add_argument("--hubspot-first-property",
                     help="the property holding the author's first name "
                          "(subfolder mode)")
    ini.add_argument("--hubspot-last-property",
                     help="the property holding the author's last name "
                          "(subfolder mode)")
    ini.add_argument("--enable-hubspot", action="store_true",
                     help="gate new manuscripts on a HubSpot record; asks for "
                          "any field left out")
    ini.add_argument("--disable-hubspot", action="store_true",
                     help="stop gating on HubSpot")
    ini.add_argument("--hubspot-object",
                     help="the objectType: deals, contacts, or a custom id")
    ini.add_argument("--hubspot-key-property",
                     help="the property a filename key is matched against")
    ini.add_argument("--hubspot-key-pattern",
                     help="a regex to pull the key out of the filename "
                          "(default: the whole filename stem)")
    ini.add_argument("--hubspot-status-property",
                     help="the dropdown property a book moves through, e.g. a "
                          "'DocProof' property (use its internal name)")
    ini.add_argument("--hubspot-format-ready-value",
                     help="the status value meaning 'format this now', e.g. "
                          "'Ready for Formatting' (the option's internal value)")
    ini.add_argument("--hubspot-format-done-value",
                     help="the status value DocProof sets once the formatted "
                          "file is back, e.g. 'Formatting Complete'")
    ini.add_argument("--hubspot-output-property",
                     help="optional: a property to write the output filename "
                          "into")
    ini.add_argument("--enable-proofing", action="store_true",
                     help="also proofread books flagged 'Ready for Proofing' "
                          "on the same status property (needs HubSpot on)")
    ini.add_argument("--disable-proofing", action="store_true",
                     help="stop proofreading (the default)")
    ini.add_argument("--proof-runner", choices=["app", "external"],
                     help="who reads the book: 'app' (DocWatch runs the galley "
                          "job itself) or 'external' (a practitioner does, and "
                          "DocWatch waits for '<surname> - book 1 - "
                          "outcome.json' to appear beside the book)")
    ini.add_argument("--hubspot-proof-ready-value",
                     help="the status value meaning 'proofread this now' "
                          "(default 'Ready for Proofing')")
    ini.add_argument("--hubspot-proof-done-value",
                     help="the status value DocProof sets once the proofread "
                          "is back and clean (default 'Proofing Complete')")
    ini.add_argument("--proof-tier", choices=["T0", "T1", "T2", "T3", "T4"],
                     help="how hard the app runner reads (default T2)")
    ini.add_argument("--proof-budget", type=float,
                     help="dollars one book's proofread may cost; 0 uses the "
                          "tier's own default")
    ini.add_argument("--hubspot-read-only", dest="hubspot_write_back",
                     action="store_false", default=None,
                     help="gate on HubSpot but never write back to it (a book "
                          "still marks itself done in Drive)")
    ini.add_argument("--hubspot-write-back", dest="hubspot_write_back",
                     action="store_true", default=None,
                     help="(default) move the record on once the file is back")

    ht = sub.add_parser("hubspot-token",
                        help="store the HubSpot private-app token")
    ht.add_argument("--status", action="store_true",
                    help="say whether a token is stored, without storing one")

    on = sub.add_parser("once", help="do one pass over the folder now")
    on.add_argument("--dry-run", action="store_true",
                    help="say what a pass would do, without downloading, "
                         "preparing or uploading anything")
    on.add_argument("--mock-tags", action="store_true",
                    help="do the whole round trip with no model call, to "
                         "rehearse the Drive side without spending anything")

    sub.add_parser("status", help="what the watcher has done lately")

    sch = sub.add_parser("schedule",
                         help="have macOS run a pass a few times a day")
    sch.add_argument("--times", default=",".join(schedulelib.DEFAULT_TIMES),
                     help="when to run, as HH:MM separated by commas "
                          f"(default: {','.join(schedulelib.DEFAULT_TIMES)})")

    sub.add_parser("unschedule", help="stop running passes automatically")

    args = ap.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else default_watch_home()
    _logging(home, verbose=args.verbose)
    return {"auth": cmd_auth, "init": cmd_init, "once": cmd_once,
            "status": cmd_status, "schedule": cmd_schedule,
            "unschedule": cmd_unschedule,
            "hubspot-token": cmd_hubspot_token}[args.cmd](args, home)


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
        authlib.sign_in(home, client_id, client_secret,
                        open_browser=_open_browser)
    except DriveError as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE

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
    if args.auto_ticks is not None:
        ws.auto_ticks = args.auto_ticks
    if args.at_times is not None:
        # A blank value clears the fixed times and hands --every its job back;
        # anything else is parsed and normalised so the panel and the CLI agree.
        try:
            times = schedulelib.parse_times(args.at_times) if args.at_times.strip() else []
        except schedulelib.ScheduleError as e:
            print(f"error: {e}", file=sys.stderr)
            return UNUSABLE
        ws.tick_at_times = [f"{h:02d}:{m:02d}" for h, m in times]
    if args.timezone is not None:
        tz = args.timezone.strip()
        if tz:
            try:
                dailylib.zone(tz)
            except schedulelib.ScheduleError as e:
                print(f"error: {e}", file=sys.stderr)
                return UNUSABLE
        ws.tick_timezone = tz
    if args.every is not None:
        if not (5 <= args.every <= 1440):
            print("error: --every is minutes, between 5 and 1440.",
                  file=sys.stderr)
            return UNUSABLE
        ws.tick_every_minutes = args.every
    if args.notify_email is not None:
        ws.notify_email = args.notify_email
    if args.notify_on_complete is not None:
        ws.notify_on_complete = args.notify_on_complete
    _apply_hubspot(args, ws)
    _apply_proofing(args, ws)
    _apply_subfolders(args, ws)
    ws.save(home)

    print(f"Watching folder {ws.folder_id or '— not set yet'}")
    print(f"Preparing with {ws.model}, handing back: {ws.prep_output}")
    if ws.auto_ticks:
        if ws.tick_at_times:
            where = f" ({ws.tick_timezone})" if ws.tick_timezone else ""
            print(f"Looking while open at {', '.join(ws.tick_at_times)}{where}")
        else:
            print(f"Looking while open every {ws.tick_every_minutes} minutes")
    if ws.notify_email:
        on_complete = (" (and a full log on every finished job)"
                       if ws.notify_on_complete else "")
        print(f"Emailing {ws.notify_email} when a pass needs a person"
              f"{on_complete}")
    if ws.subfolders_enabled:
        print(f"Routing into per-author subfolders of {ws.folder_id}, named "
              f"from {ws.hubspot_first_property or '— first not set'} / "
              f"{ws.hubspot_last_property or '— last not set'}")
    if ws.require_source_label:
        print("Preparing only '<surname> - Book Original'")
    if ws.hubspot_enabled:
        print(f"HubSpot gate on: {ws.hubspot_object}, "
              f"{ws.hubspot_status_property or '— property not set'}: "
              f"'{ws.hubspot_format_ready_value or '— not set'}' → "
              f"'{ws.hubspot_format_done_value or '— not set'}'"
              + ("  (READ-ONLY: no write-back)"
                 if not ws.hubspot_write_back else ""))
    if ws.proofing_enabled:
        who = ("DocWatch reads the book" if ws.proof_runner == "app"
               else "a practitioner reads the book; DocWatch waits for the "
                    "hand-off")
        print(f"Proofing on: "
              f"'{ws.hubspot_proof_ready_value or '— not set'}' → "
              f"'{ws.hubspot_proof_done_value or '— not set'}' ({who})")
        if ws.proof_runner == "app":
            budget = ws.proof_budget_usd or "the tier default"
            print(f"  reading at {ws.proof_tier}, up to {budget} per book")
    print(f"Keeping its things in {home}")
    missing = _missing(ws)
    if missing:
        print(f"\nStill needed: {missing}")
        return UNUSABLE
    print("\nReady. Try `docproof-watch once --dry-run` to see what it would "
          "do.")
    return OK


_HUBSPOT_FLAGS = (
    ("hubspot_object", "hubspot_object"),
    ("hubspot_key_property", "hubspot_key_property"),
    ("hubspot_first_property", "hubspot_first_property"),
    ("hubspot_last_property", "hubspot_last_property"),
    ("hubspot_key_pattern", "hubspot_key_pattern"),
    ("hubspot_status_property", "hubspot_status_property"),
    ("hubspot_format_ready_value", "hubspot_format_ready_value"),
    ("hubspot_format_done_value", "hubspot_format_done_value"),
    ("hubspot_output_property", "hubspot_output_property"),
)

# The fields a gate cannot run without. Asked for interactively when enabling
# leaves one blank, so `init --enable-hubspot` never quietly writes a gate the
# next pass will only refuse.
_HUBSPOT_REQUIRED = {
    "hubspot_object": "Which HubSpot object (deals, contacts, or a custom id)",
    "hubspot_key_property": "Which property the filename key matches",
    "hubspot_status_property": "Which dropdown property a book moves through",
    "hubspot_format_ready_value": "Which value means 'ready to format'",
    "hubspot_format_done_value": "Which value means 'formatting complete'",
}


def _apply_hubspot(args, ws: WatchSettings) -> None:
    """Fold the `--hubspot-*` flags into the settings, and fill any required
    field still blank once the gate is switched on."""
    if getattr(args, "disable_hubspot", False):
        ws.hubspot_enabled = False
    for attr, flag in _HUBSPOT_FLAGS:
        value = getattr(args, flag, None)
        if value is not None:
            setattr(ws, attr, value)
    if getattr(args, "enable_hubspot", False):
        ws.hubspot_enabled = True
    if getattr(args, "hubspot_write_back", None) is not None:
        ws.hubspot_write_back = args.hubspot_write_back
    if not ws.hubspot_enabled:
        return
    for attr, prompt in _HUBSPOT_REQUIRED.items():
        if not getattr(ws, attr):
            setattr(ws, attr, _ask(prompt))


_PROOF_FLAGS = (
    ("proof_runner", "proof_runner"),
    ("hubspot_proof_ready_value", "hubspot_proof_ready_value"),
    ("hubspot_proof_done_value", "hubspot_proof_done_value"),
    ("proof_tier", "proof_tier"),
)

# The fields proofing cannot run without. Both ship with real defaults, so this
# only ever fires for somebody who deliberately blanked one.
_PROOF_REQUIRED = {
    "hubspot_proof_ready_value": "Which value means 'ready to proofread'",
    "hubspot_proof_done_value": "Which value means 'proofing complete'",
}


def _apply_proofing(args, ws: WatchSettings) -> None:
    """Fold the `--*-proofing` / `--proof-*` flags in, and fill any required
    field left blank once the stage is switched on — the posture the HubSpot
    gate takes, for the same reason: a stage that cannot ask, or cannot answer,
    is worse than one that is off."""
    if getattr(args, "disable_proofing", False):
        ws.proofing_enabled = False
    for attr, flag in _PROOF_FLAGS:
        value = getattr(args, flag, None)
        if value is not None:
            setattr(ws, attr, value)
    if getattr(args, "proof_budget", None) is not None:
        if args.proof_budget < 0:
            print("note: --proof-budget cannot be negative; leaving it alone.")
        else:
            ws.proof_budget_usd = float(args.proof_budget)
    if getattr(args, "enable_proofing", False):
        ws.proofing_enabled = True
    if not ws.proofing_enabled:
        return
    if not ws.hubspot_enabled:
        print("Note: proofing needs the HubSpot gate on — the book is flagged "
              "for it on a CRM property. Enable it with --enable-hubspot.")
    for attr, prompt in _PROOF_REQUIRED.items():
        if not getattr(ws, attr):
            setattr(ws, attr, _ask(prompt))


# The name properties subfolder mode cannot resolve a folder without.
_SUBFOLDER_REQUIRED = {
    "hubspot_first_property": "Which property holds the author's first name",
    "hubspot_last_property": "Which property holds the author's last name",
}


def _apply_subfolders(args, ws: WatchSettings) -> None:
    """Fold the subfolder flags in, and fill the name properties when the mode
    is switched on and left blank — the same posture the HubSpot gate takes."""
    if getattr(args, "disable_subfolders", False):
        ws.subfolders_enabled = False
    if getattr(args, "enable_subfolders", False):
        ws.subfolders_enabled = True
    if getattr(args, "require_source_label", None) is not None:
        ws.require_source_label = args.require_source_label
    if not ws.subfolders_enabled:
        return
    if not ws.hubspot_enabled:
        print("Note: subfolders need the HubSpot gate on — the folder name "
              "comes from the record. Enable it with --enable-hubspot.",
              file=sys.stderr)
    for attr, prompt in _SUBFOLDER_REQUIRED.items():
        if not getattr(ws, attr):
            setattr(ws, attr, _ask(prompt))


# What is still needed, said the way a terminal should say it. The library
# answers with a word; naming the command that fixes it is this front end's job.
_NEEDS = {"folder": "a folder to watch — `docproof-watch init --folder <address>`",
          "auth": "a Google sign-in — `docproof-watch auth`"}


def cmd_hubspot_token(args, home: Path) -> int:
    """Store the HubSpot private-app token where the vendor keys live.

    The token is read off stdin when one is piped in, and asked for without an
    echo otherwise, so it never lands in shell history the way a `--flag` would.
    Private-app tokens do not expire, so this is a one-time step per install."""
    if args.status:
        stored = bool(get_api_key(HUBSPOT_KEY))
        print("A HubSpot token is stored." if stored
              else "No HubSpot token yet. Run `docproof-watch hubspot-token`.")
        return OK if stored else UNUSABLE

    token = _read_secret("Paste the HubSpot private-app token")
    if not token:
        print("error: no token given, so nothing was stored.", file=sys.stderr)
        return UNUSABLE
    set_api_key(HUBSPOT_KEY, token)
    print("Stored. It is in your Keychain, not in a file.")
    if not WatchSettings.load(home).hubspot_enabled:
        print("The HubSpot gate is off. Turn it on with "
              "`docproof-watch init --enable-hubspot`.")
    return OK


def _read_secret(prompt: str) -> str:
    """A pasted secret, off stdin when piped and without an echo otherwise."""
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _missing(ws: WatchSettings) -> str:
    # The reader is handed over rather than left to the library to find: this
    # module is where a terminal's idea of "where the keys are" lives.
    return _NEEDS.get(statuslib.missing(ws, get_key=get_api_key), "")


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
    for name, reason in report.needs_human:
        print(f"Needs a person — {name}: {reason}", file=sys.stderr)
    for name, reason in report.missing_source:
        print(f"No Book Original — {name}: {reason}", file=sys.stderr)
    for name, reason in report.stuck_ready:
        print(f"Ready but already formatted — {name}: {reason}", file=sys.stderr)
    if not (report.prepped or report.failed or report.needs_human
            or report.missing_source or report.stuck_ready):
        print(f"Nothing new in the folder ({report.listed} file(s) looked at).")
    return OK if report.ok else PARTIAL


# The same words the panel uses; one copy, in the library.
_PLAIN = statuslib.PLAIN_STAGE


# --- what it has been doing ---------------------------------------------------

def cmd_schedule(args, home: Path) -> int:
    ws = WatchSettings.load(home)
    try:
        times = schedulelib.parse_times(args.times)
        target = schedulelib.install(times, home)
    except schedulelib.ScheduleError as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE

    print(f"DocProof will look in the folder at "
          f"{schedulelib.describe(times)}, every day.")
    print(f"  {target}")
    print(f"  output goes to {home / schedulelib.LAUNCHD_LOG}")
    # Said plainly rather than discovered in three weeks: launchd does not run
    # a calendar job while the Mac is asleep, and does not go back for the one
    # it missed. It runs at the next scheduled time the machine is awake for.
    print("\nA sleeping Mac runs nothing. A missed time is not made up "
          "later — the next one that finds the machine awake does the work.")
    missing = _missing(ws)
    if missing:
        print(f"\nNote: nothing will happen until there is {missing}")
    return OK


def cmd_unschedule(args, home: Path) -> int:
    try:
        removed = schedulelib.uninstall()
    except schedulelib.ScheduleError as e:
        print(f"error: {e}", file=sys.stderr)
        return UNUSABLE
    print("DocProof will not look in the folder on its own any more."
          if removed else "There was no schedule to remove.")
    return OK


def cmd_status(args, home: Path) -> int:
    """The same account the panel draws, printed.

    Everything below is `status()`; this function is only the terminal's way of
    saying it."""
    s = statuslib.status(home, get_key=get_api_key)
    print(f"Folder:  {s['folder_id'] or '— not set yet'}")
    print(f"Model:   {s['model']}")
    print(f"Home:    {s['home']}")
    print(f"Signed in: {'yes' if s['signed_in'] else 'no'}")
    # Two clocks: the launchd agent (runs while a Mac is closed) and the in-app
    # one (runs while DocProof or the server is up). The second is the only one
    # on the server, so it is said even when the first is unset.
    print(f"Runs at (while closed): "
          f"{', '.join(s['times']) or '— only when you say so'}")
    if s["auto_ticks"]:
        if s["tick_at_times"]:
            where = f" ({s['tick_timezone']})" if s["tick_timezone"] else ""
            nxt = s.get("next_tick_at")
            after = f", next {nxt}" if nxt else ""
            print(f"Runs at (while open):   "
                  f"{', '.join(s['tick_at_times'])}{where}{after}")
        else:
            print(f"Runs at (while open):   "
                  f"every {s['tick_every_minutes']} minutes")
    else:
        print("Runs at (while open):   — off")
    if s["missing"]:
        print(f"\nStill needed: {_NEEDS[s['missing']]}")

    if not s["files"]:
        print("\nNothing has been prepared yet.")
        return OK

    print(f"\n{len(s['files'])} manuscript(s) seen:\n")
    for row in s["files"]:
        cost = f"  ${row['cost']:.2f}" if row["cost"] else ""
        print(f"  {row['said']:<12} {row['name']}{cost}")
        for name in row["uploaded"]:
            print(f"               → {name}")
    return OK
