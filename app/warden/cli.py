"""`docproof-warden` — the command line the Warden itself runs from launchd,
and the one a person (or the harness) runs by hand.

Every subcommand is a thin wrapper over the deterministic modules in this
package: `init`/`install`/`uninstall` set the Mini up, `snapshot`/`check`/
`status` are read-only, `tick`/`listen` are what launchd calls on a clock,
and `say`/`email`/`request`/`verb`/`code-request`/`code-approved` are the
same doors the model uses from inside a harness turn — so a person debugging
by hand and the harness running headless are indistinguishable to the rest
of the system.

No subcommand ever accepts a secret value as an argument — `secret set NAME`
reads the value from stdin, because an argv is visible in `ps` and a shell
history file, and a secret typed there is a secret leaked."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.warden import approvals, config as config_module, install as install_module
from app.warden import names, rules, secrets as secrets_module, snapshot as snapshot_module
from app.warden import tick as tick_module
from app.warden import verbs
from app.warden.journal import Journal, RequestPending
from app.warden.messaging import gmail, imessage

EASTERN = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _home(args: argparse.Namespace) -> Path:
    override = getattr(args, "home", None)
    return Path(override) if override else config_module.home()


def _journal(home: Path) -> Journal:
    return Journal(home / "journal.sqlite")


def _load_config(home: Path) -> config_module.WardenConfig:
    return config_module.WardenConfig.load(home)


def _load_latest_snapshot(home: Path) -> dict:
    path = home / "snapshots" / "latest.json"
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _eastern(value: str | None) -> str:
    if not value:
        return "never"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %-I:%M %p ET")


# --------------------------------------------------------------------------
# init / status
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    home = _home(args)
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / config_module.CONFIG_FILE
    if cfg_path.is_file():
        print(f"{cfg_path} already exists; leaving it alone.")
    else:
        config_module.WardenConfig().save(home)
        print(f"Wrote {cfg_path}.")

    missing = secrets_module.missing(list(secrets_module.ENV_VARS))
    if missing:
        print("Still missing (set with `docproof-warden secret set NAME`, "
              "or the matching environment variable):")
        for name in missing:
            print(f"  {name}  (env: {secrets_module.ENV_VARS[name]})")
    else:
        print("Every known secret is configured.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    home = _home(args)
    snap = _load_latest_snapshot(home)
    with _journal(home) as journal:
        docwatch = snap.get("docwatch") or {}
        agent = docwatch.get("agent") or {}
        paused = journal.get("paused") in ("1", "true", "True")
        quiet_until = journal.get("quiet_until")
        open_findings = journal.open_findings()
        open_requests = journal.open_requests()

        print(f"Warden {snap.get('warden_version', '?')}  "
              f"({'paused' if paused else 'active'})")
        print(f"  snapshot taken: {_eastern(snap.get('at'))}")
        print(f"  Galley: {agent.get('state', 'unknown')} on "
              f"{agent.get('book') or 'no book'} (phase {agent.get('phase') or '-'})")
        ledger = agent.get("ledger") or {}
        claimed = ledger.get("claimed") or []
        if claimed:
            print(f"  claimed: {', '.join(c.get('name', c.get('file_id', '?')) for c in claimed)}")
        print(f"  DocWatch last tick: {_eastern(docwatch.get('last_tick'))}")
        if quiet_until:
            print(f"  quiet until: {_eastern(quiet_until)}")
        print(f"  {len(open_findings)} open finding(s):")
        for f in open_findings:
            print(f"    [{f.severity}] {f.rule} — {f.summary}")
        print(f"  {len(open_requests)} open request(s):")
        for r in open_requests:
            print(f"    #{r.number} ({r.kind}) {r.text} — expires "
                  f"{_eastern(r.expires_at)}")
    return 0


# --------------------------------------------------------------------------
# snapshot / check
# --------------------------------------------------------------------------

def cmd_snapshot(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        snap = snapshot_module.collect(cfg, secrets=secrets_module,
                                       run=subprocess.run,
                                       opener=urllib.request.urlopen,
                                       now=_now(), journal=journal)
        path = snapshot_module.save(home, snap)
    print(f"Wrote {path}.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    snap = _load_latest_snapshot(home)
    if not snap:
        print("No snapshot yet — run `docproof-warden snapshot` first.",
             file=sys.stderr)
        return 1
    findings = list(rules.evaluate(snap, cfg.thresholds, now=_now()))
    findings += names.as_findings(names.compare(snap))
    if not findings:
        print("Nothing is stuck.")
        return 0
    for f in findings:
        print(f"[{f.severity}] T{f.tier} {f.rule} ({f.key}): {f.summary}")
    return 0


# --------------------------------------------------------------------------
# tick / listen
# --------------------------------------------------------------------------

def cmd_tick(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        result = tick_module.run_tick(
            cfg, secrets=secrets_module, journal=journal, run=subprocess.run,
            opener=urllib.request.urlopen, now=_now(), home=home,
            dry_run=args.dry_run, allow_model=not args.no_model)
    print(f"{len(result.findings)} finding(s), "
         f"{'1 action' if result.action else 'no action'}, "
         f"{len(result.needs_model)} needing the model.")
    if result.action:
        print(f"  action: {result.action}")
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        result = tick_module.listen(
            cfg, secrets=secrets_module, journal=journal, run=subprocess.run,
            opener=urllib.request.urlopen, now=_now(), home=home)
    print(f"Handled {len(result.handled)} message(s).")
    return 0


# --------------------------------------------------------------------------
# say / email
# --------------------------------------------------------------------------

def _cli_say(cfg, journal, run, text: str, *, high: bool, now: datetime) -> tuple[bool, str]:
    if not cfg.imessage_enabled or not cfg.owner_handle:
        return False, "iMessage is disabled, or no owner_handle is configured."
    if not high and tick_module.in_quiet_hours(cfg, journal, now):
        return False, "It's quiet hours; pass --high to send anyway."
    if not imessage.rate_ok(journal, cfg.max_texts_per_hour):
        return False, f"Already sent {cfg.max_texts_per_hour} text(s) this hour."
    sent = imessage.send(cfg.owner_handle, text, run=run, journal=journal,
                         max_per_hour=cfg.max_texts_per_hour)
    if not sent:
        return False, "osascript refused to send the message."
    journal.record_message("imessage", "out", cfg.owner_handle, text)
    return True, "Sent."


def cmd_say(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        ok, message = _cli_say(cfg, journal, subprocess.run, args.text,
                               high=args.high, now=_now())
    print(message)
    return 0 if ok else 1


def cmd_email(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        sent = gmail.send_team(cfg, secrets_module, args.subject, args.body,
                               opener=urllib.request.urlopen)
        for addr in sent:
            journal.record_message("email", "out", addr, args.body, ref=args.subject)
    if not sent:
        print("No team_emails configured; nothing sent.", file=sys.stderr)
        return 1
    print(f"Sent to {', '.join(sent)}.")
    return 0


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------

def cmd_request(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    with _journal(home) as journal:
        if args.request_action == "list":
            for r in journal.open_requests():
                print(f"#{r.number} ({r.kind}) {r.text} — expires "
                     f"{_eastern(r.expires_at)}")
            return 0

        number = args.number
        answer = "yes" if args.request_action == "yes" else "no"
        now = _now()
        req = approvals.apply_answer(journal, f"{answer} {number}", cfg.owner_name, now)
        if req is None:
            print(f"#{number} isn't open (already answered, expired, or "
                 f"doesn't exist).", file=sys.stderr)
            return 1
        print(f"#{number} marked {answer}.")
        if answer == "yes" and req.payload.get("verb"):
            snap = _load_latest_snapshot(home)
            ctx = verbs.VerbContext(config=cfg, secrets=secrets_module, journal=journal,
                                    snapshot=snap, run=subprocess.run,
                                    opener=urllib.request.urlopen, now=now)
            result = verbs.run_verb(ctx, req.payload["verb"], req.payload.get("args") or {},
                                    False, finding_id=req.payload.get("finding_id"))
            print(f"  {result.message}")
    return 0


# --------------------------------------------------------------------------
# verb
# --------------------------------------------------------------------------

def _parse_kv(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"expected key=value, got {pair!r}")
        out[key] = value
    return out


def cmd_verb(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    snap = _load_latest_snapshot(home)
    call_args = _parse_kv(args.args)
    with _journal(home) as journal:
        ctx = verbs.VerbContext(config=cfg, secrets=secrets_module, journal=journal,
                                snapshot=snap, run=subprocess.run,
                                opener=urllib.request.urlopen, now=_now())
        result = verbs.run_verb(ctx, args.name, call_args, args.dry_run)
    print(result.message)
    if result.result:
        print(json.dumps(result.result, indent=2, default=str))
    return 0 if result.ok else 1


# --------------------------------------------------------------------------
# install / uninstall
# --------------------------------------------------------------------------

def cmd_install(args: argparse.Namespace) -> int:
    home = _home(args)
    agents_dir = Path(args.agents_dir) if getattr(args, "agents_dir", None) else None
    tick_plist, listen_plist = install_module.install(
        home=home, interval_min=args.interval, listen_interval_min=args.listen_interval,
        agents_dir=agents_dir)
    print(f"Wrote {tick_plist}\nWrote {listen_plist}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    agents_dir = Path(args.agents_dir) if getattr(args, "agents_dir", None) else None
    removed = install_module.uninstall(agents_dir=agents_dir)
    if removed:
        for path in removed:
            print(f"Removed {path}")
    else:
        print("Nothing was installed.")
    return 0


# --------------------------------------------------------------------------
# secret
# --------------------------------------------------------------------------

def cmd_secret(args: argparse.Namespace) -> int:
    if args.secret_action == "set":
        value = sys.stdin.readline().rstrip("\n")
        if not value:
            print("No value on stdin; nothing stored.", file=sys.stderr)
            return 1
        secrets_module.set(args.name, value)
        print(f"Stored {args.name}.")
        return 0
    if args.secret_action == "delete":
        secrets_module.delete(args.name)
        print(f"Removed {args.name}.")
        return 0
    # list
    missing = set(secrets_module.missing(list(secrets_module.ENV_VARS)))
    for name in secrets_module.ENV_VARS:
        print(f"{name}: {'missing' if name in missing else 'configured'}")
    return 0


# --------------------------------------------------------------------------
# code-request / code-approved
# --------------------------------------------------------------------------

_LAST_CODE_REQUEST_KEY = "last_code_request_number"


def cmd_code_request(args: argparse.Namespace) -> int:
    home = _home(args)
    cfg = _load_config(home)
    now = _now()
    text = f"Code fix for {args.rule}: {args.cause}"
    payload = {"rule": args.rule, "cause": args.cause, "files": args.files,
              "fix": args.fix}
    with _journal(home) as journal:
        try:
            req = journal.open_request(
                "code", text, payload,
                expires_at=now + timedelta(hours=cfg.thresholds.request_expiry_h))
        except RequestPending as e:
            print(f"Refused: {e}", file=sys.stderr)
            return 1
        journal.set(_LAST_CODE_REQUEST_KEY, str(req.number))
        _cli_say(cfg, journal, subprocess.run, approvals.request_text(req),
                high=True, now=now)
    print(f"Opened code request #{req.number}.")
    return 0


def cmd_code_approved(args: argparse.Namespace) -> int:
    home = _home(args)
    with _journal(home) as journal:
        number = journal.get(_LAST_CODE_REQUEST_KEY)
        if not number:
            print("No code request has been opened yet.", file=sys.stderr)
            return 1
        req = journal.get_request(int(number))
    if req is None or req.status != "yes":
        status = req.status if req is not None else "unknown"
        print(f"#{number} is not approved (status: {status}).", file=sys.stderr)
        return 1
    print(json.dumps(req.payload))
    return 0


# --------------------------------------------------------------------------
# argument parser
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docproof-warden")
    parser.add_argument("--home", default=None,
                        help="Warden home directory (default: $WARDEN_HOME or "
                             "~/.docproof-warden)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="write warden.yaml if absent; report missing secrets").set_defaults(func=cmd_init)
    sub.add_parser("snapshot", help="collect and save one snapshot").set_defaults(func=cmd_snapshot)
    sub.add_parser("check", help="evaluate the latest snapshot and print findings").set_defaults(func=cmd_check)
    sub.add_parser("status", help="one-screen human summary, times in Eastern").set_defaults(func=cmd_status)

    p_tick = sub.add_parser("tick", help="run one full tick")
    p_tick.add_argument("--no-model", action="store_true")
    p_tick.add_argument("--dry-run", action="store_true")
    p_tick.set_defaults(func=cmd_tick)

    sub.add_parser("listen", help="read inbound iMessages/email replies once").set_defaults(func=cmd_listen)

    p_say = sub.add_parser("say", help="text the owner")
    p_say.add_argument("text")
    p_say.add_argument("--high", action="store_true",
                       help="bypass quiet hours (rate cap still applies)")
    p_say.set_defaults(func=cmd_say)

    p_email = sub.add_parser("email", help="email the team")
    p_email.add_argument("--subject", required=True)
    p_email.add_argument("--body", required=True)
    p_email.set_defaults(func=cmd_email)

    p_request = sub.add_parser("request", help="list or answer numbered requests")
    req_sub = p_request.add_subparsers(dest="request_action", required=True)
    req_sub.add_parser("list")
    p_yes = req_sub.add_parser("yes")
    p_yes.add_argument("number", type=int)
    p_no = req_sub.add_parser("no")
    p_no.add_argument("number", type=int)
    p_request.set_defaults(func=cmd_request)

    p_verb = sub.add_parser("verb", help="run one verb directly")
    p_verb.add_argument("name")
    p_verb.add_argument("--dry-run", action="store_true")
    p_verb.add_argument("args", nargs="*", help="key=value pairs")
    p_verb.set_defaults(func=cmd_verb)

    p_install = sub.add_parser("install", help="write and load the launchd plists")
    p_install.add_argument("--interval", type=int, default=20)
    p_install.add_argument("--listen-interval", type=int, default=2)
    p_install.add_argument("--agents-dir", default=None,
                           help=argparse.SUPPRESS)  # test seam; real installs use ~/Library/LaunchAgents
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="unload and remove the launchd plists")
    p_uninstall.add_argument("--agents-dir", default=None, help=argparse.SUPPRESS)
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_secret = sub.add_parser("secret", help="manage Keychain secrets")
    sec_sub = p_secret.add_subparsers(dest="secret_action", required=True)
    p_set = sec_sub.add_parser("set", help="read the value from stdin")
    p_set.add_argument("name")
    p_del = sec_sub.add_parser("delete")
    p_del.add_argument("name")
    sec_sub.add_parser("list")
    p_secret.set_defaults(func=cmd_secret)

    p_code_req = sub.add_parser("code-request", help="ask to write code")
    p_code_req.add_argument("--rule", required=True)
    p_code_req.add_argument("--cause", required=True)
    p_code_req.add_argument("--files", required=True)
    p_code_req.add_argument("--fix", required=True)
    p_code_req.set_defaults(func=cmd_code_request)

    sub.add_parser("code-approved", help="print the approved code request, or exit 1").set_defaults(func=cmd_code_approved)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":                                     # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
