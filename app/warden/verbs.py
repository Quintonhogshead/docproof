"""Every fix the Warden may make, in one place, behind one call shape.

`docs/monitoring-agent-plan.md` draws the line between "reads and takes
snapshots" (always allowed), Tier 0 ("on its own, logged"), Tier 1 ("after a
yes from Quinton"), and Tier 2 ("never" — reported only, no verb exists for
these at all). Every verb here is Tier 0 or Tier 1; there is deliberately no
Tier-2 verb to call, because the whole point of that tier is that nothing in
this file may do it.

A verb is a plain function `(ctx, *, dry_run, **args) -> VerbResult`. It never
touches `subprocess` or `urllib` directly — every process it runs goes through
`ctx.run`, every HTTP call through `ctx.opener` — so a test can hand it a fake
of either and never let a verb near a real Fly machine, a real Keychain
token, or a real HubSpot record. `REGISTRY` is the whole vocabulary the CLI's
`verb NAME`, the tick's Tier-0 loop, and `listen()`'s "yes N" all dispatch
through, and `run_verb` is the one place that (a) enforces the claimed-book
guard, (b) calls the verb, and (c) journals what happened — so no caller can
run a verb and forget to record it.

The claimed-book guard (`needs_idle_agent`) is the code form of
"never deploy mid-run": a verb marked with it refuses outright while
Galley's heartbeat says a book is claimed or running. `galley-nudge` is
deliberately NOT marked this way — its whole job is un-sticking a run that
*is* claimed — so it carries its own, narrower guard instead: it will not
touch a book unless the journal already holds an open `agent-silent`,
`agent-stalled`, or `book-abandoned` finding naming it, i.e. the run has
already been judged provably dead by a rule, not by this verb's own opinion.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("docproof.app.warden.verbs")

#: Galley states in which a book is actively being worked — the claimed-book
#: guard's other half is the ledger's `claimed` list, checked separately,
#: because a book can be claimed while the agent is momentarily `idle`
#: between polls.
_BUSY_STATES = ("running", "finishing", "stopping")

#: Rules whose Finding proves a run is dead enough for `galley-nudge` to
#: touch it without a human's yes.
_STALL_RULES = ("agent-silent", "agent-stalled", "book-abandoned")

#: The InDesign worker's login-agent label, used by `indesign-restart` after
#: it has quit (or force-killed) the application itself.
_INDESIGN_WORKER_LABEL = "com.docproof.interior-review-worker"

#: HubSpot's Projects object.
_PROJECTS_OBJECT = "0-970"


@dataclass
class VerbContext:
    """Everything a verb needs, and nothing it may reach around.

    `run` defaults to `subprocess.run` and `opener` to `urllib.request.urlopen`
    so production code can build one with just the first four fields; tests
    override both with fakes. `now` is optional — verbs that care about wall
    clock (`galley-resume`, checking a pause's `resume_after`) fall back to
    `datetime.now(timezone.utc)` when it is not given, but a caller that wants
    a fixed, injectable clock (every test) should always pass it."""

    config: Any
    secrets: Any
    journal: Any
    snapshot: dict
    run: Callable[..., Any] = subprocess.run
    opener: Callable[..., Any] = urllib.request.urlopen
    now: datetime | None = None

    def moment(self) -> datetime:
        return self.now or datetime.now(timezone.utc)


@dataclass
class VerbResult:
    ok: bool
    before: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    message: str = ""


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------

def _agent(snapshot: dict) -> dict:
    agent = (snapshot.get("docwatch") or {}).get("agent")
    return agent if isinstance(agent, dict) else {}


def _agent_busy(snapshot: dict) -> bool:
    agent = _agent(snapshot)
    if agent.get("state") in _BUSY_STATES:
        return True
    ledger = agent.get("ledger") or {}
    return bool(ledger.get("claimed"))


def _run_cmd(ctx: VerbContext, cmd: list[str], *, timeout: int = 60) -> dict:
    """Run one command through `ctx.run`. Never raises — a failed command is
    `{"ok": False, ...}`, not an exception a verb has to catch itself."""
    try:
        proc = ctx.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:                                     # noqa: BLE001
        return {"cmd": cmd, "ok": False, "error": str(e)}
    ok = getattr(proc, "returncode", 1) == 0
    return {"cmd": cmd, "ok": ok, "returncode": getattr(proc, "returncode", None),
            "stdout": (getattr(proc, "stdout", "") or "")[-4000:],
            "stderr": (getattr(proc, "stderr", "") or "")[-4000:]}


def _fly_bin(ctx: VerbContext) -> str:
    return getattr(ctx.config, "fly_bin", "") or "fly"


def _fly_app(ctx: VerbContext) -> str:
    return getattr(ctx.config, "fly_app", "") or ""


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def _warden_post(ctx: VerbContext, path: str, body: dict) -> dict:
    """POST one of the server's four Warden routes (see
    `app/routes/watch.py`), bearer-authenticated with the `warden_token`
    secret. Never raises — a dead server is `{"ok": False, ...}`."""
    token = ctx.secrets.get("warden_token") if ctx.secrets else None
    if not token:
        return {"ok": False, "error": "no warden_token configured"}
    url = (getattr(ctx.config, "app_url", "") or "").rstrip("/") + path
    data = json.dumps(body or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with ctx.opener(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"the server answered {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"could not reach the server: {getattr(e, 'reason', e)}"}
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = {}
    return {"ok": True, "response": payload}


# --------------------------------------------------------------------------
# galley-nudge / galley-resume
# --------------------------------------------------------------------------

def _stall_finding_for(ctx: VerbContext, book: str):
    if ctx.journal is None or not book:
        return None
    needle = book.strip().lower()
    for f in ctx.journal.open_findings():
        if f.rule not in _STALL_RULES:
            continue
        if (f.key or "").strip().lower() == needle:
            return f
        evidence = f.evidence or {}
        for candidate in (evidence.get("book"), evidence.get("name")):
            if candidate and str(candidate).strip().lower() == needle:
                return f
    return None


def galley_nudge(ctx: VerbContext, *, dry_run: bool, book: str) -> VerbResult:
    """`agent --forget <book>` over `fly ssh console`, then restart the agent
    machine — only for a book a rule has already judged provably dead."""
    finding = _stall_finding_for(ctx, book)
    if finding is None:
        return VerbResult(ok=False, message=(
            f"No open agent-silent/agent-stalled/book-abandoned finding "
            f"names {book!r}; refusing to nudge a run that isn't provably "
            f"dead."))
    app = _fly_app(ctx)
    forget_cmd = [_fly_bin(ctx), "ssh", "console", "-a", app,
                  "--process-group", "agent", "-C",
                  f"docproof galley agent --forget {book}"]
    machines = (((ctx.snapshot.get("fly") or {}).get("agent") or {})
                .get("machines") or [])
    machine_id = str(machines[0].get("id")) if machines else ""
    restart_cmd = ([_fly_bin(ctx), "machine", "restart", machine_id, "-a", app]
                   if machine_id else None)
    before = {"agent": _agent(ctx.snapshot),
              "finding": {"rule": finding.rule, "key": finding.key}}
    commands = [forget_cmd] + ([restart_cmd] if restart_cmd else [])
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "commands": commands},
                          message=f"Would forget {book} and restart the agent machine.")
    forget = _run_cmd(ctx, forget_cmd, timeout=60)
    if restart_cmd:
        restart = _run_cmd(ctx, restart_cmd, timeout=30)
    else:
        restart = {"ok": False, "error": "no agent machine id in the snapshot"}
    ok = bool(forget.get("ok")) and bool(restart.get("ok"))
    return VerbResult(ok=ok, before=before, result={"forget": forget, "restart": restart},
                      message=(f"Nudged {book}." if ok
                              else f"Nudging {book} did not fully succeed; see result."))


def galley_resume(ctx: VerbContext, *, dry_run: bool) -> VerbResult:
    """Clear a past-due subscription pause. Refuses while the pause's own
    `resume_after` is still in the future — this only ever lifts a pause
    whose window has already elapsed."""
    agent = _agent(ctx.snapshot)
    pause = agent.get("usage_pause")
    if not isinstance(pause, dict) or pause.get("resume_after") is None:
        return VerbResult(ok=False, message="no active subscription pause to resume from.")
    try:
        resume_after = float(pause["resume_after"])
    except (TypeError, ValueError):
        return VerbResult(ok=False, message="the pause's resume_after is unreadable.")
    if ctx.moment().timestamp() < resume_after:
        return VerbResult(ok=False, message=(
            "the subscription pause has not reached its own resume time yet."))
    cmd = [_fly_bin(ctx), "ssh", "console", "-a", _fly_app(ctx),
           "--process-group", "agent", "-C",
           "rm -f /data/galley-workspaces/.subscription-pause.json"]
    before = {"usage_pause": pause}
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "commands": [cmd]},
                          message="Would clear the subscription pause.")
    outcome = _run_cmd(ctx, cmd, timeout=30)
    return VerbResult(ok=bool(outcome.get("ok")), before=before, result=outcome,
                      message=("Cleared the subscription pause." if outcome.get("ok")
                              else "Could not clear the pause; see result."))


# --------------------------------------------------------------------------
# docwatch-requeue / docwatch-run / resend-completion
# --------------------------------------------------------------------------

def docwatch_requeue(ctx: VerbContext, *, dry_run: bool, file_id: str) -> VerbResult:
    """The requeue recipe's server half: clear the proof flag, then run a
    pass so DocWatch looks at the book again."""
    before = {"file_id": file_id}
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True,
                                  "would": ["flags/reset stage=proof", "run"]},
                          message=f"Would reset {file_id}'s proof flag and run a pass.")
    reset = _warden_post(ctx, "/api/watch/warden/flags/reset",
                         {"file_id": file_id, "stage": "proof"})
    if not reset.get("ok"):
        return VerbResult(ok=False, before=before, result={"reset": reset},
                          message=f"Could not reset the flag: {reset.get('error', '')}")
    run_call = _warden_post(ctx, "/api/watch/warden/run", {})
    ok = bool(run_call.get("ok"))
    return VerbResult(ok=ok, before=before, result={"reset": reset, "run": run_call},
                      message=("Requeued." if ok
                              else "Reset the flag but the run call failed."))


def docwatch_run(ctx: VerbContext, *, dry_run: bool) -> VerbResult:
    if dry_run:
        return VerbResult(ok=True, result={"dry_run": True},
                          message="Would run a DocWatch pass now.")
    outcome = _warden_post(ctx, "/api/watch/warden/run", {})
    return VerbResult(ok=bool(outcome.get("ok")), result=outcome,
                      message=("Started a pass." if outcome.get("ok")
                              else f"Could not start a pass: {outcome.get('error', '')}"))


def resend_completion(ctx: VerbContext, *, dry_run: bool, file_id: str) -> VerbResult:
    before = {"file_id": file_id}
    if dry_run:
        return VerbResult(ok=True, before=before, result={"dry_run": True},
                          message=f"Would re-send the completion email for {file_id}.")
    outcome = _warden_post(ctx, "/api/watch/warden/resend-completion",
                           {"file_id": file_id})
    return VerbResult(ok=bool(outcome.get("ok")), before=before, result=outcome,
                      message=("Re-sent the completion email." if outcome.get("ok")
                              else f"Could not re-send: {outcome.get('error', '')}"))


# --------------------------------------------------------------------------
# fly-restart-app / fly-restart-agent
# --------------------------------------------------------------------------

def _restart_group(ctx: VerbContext, group: str, *, dry_run: bool) -> VerbResult:
    machines = ((ctx.snapshot.get("fly") or {}).get(group) or {}).get("machines") or []
    ids = [str(m.get("id")) for m in machines if m.get("id")]
    before = {"group": group, "machines": machines}
    if not ids:
        return VerbResult(ok=False, before=before,
                          message=f"no {group}-group machine id in the snapshot.")
    app = _fly_app(ctx)
    commands = [[_fly_bin(ctx), "machine", "restart", mid, "-a", app] for mid in ids]
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "commands": commands},
                          message=f"Would restart {len(ids)} {group}-group machine(s).")
    outcomes = [_run_cmd(ctx, cmd, timeout=30) for cmd in commands]
    ok = all(o.get("ok") for o in outcomes)
    return VerbResult(ok=ok, before=before, result={"restarts": outcomes},
                      message=(f"Restarted the {group} machine(s)." if ok
                              else f"Restarting the {group} machine(s) had failures."))


def fly_restart_app(ctx: VerbContext, *, dry_run: bool) -> VerbResult:
    return _restart_group(ctx, "app", dry_run=dry_run)


def fly_restart_agent(ctx: VerbContext, *, dry_run: bool) -> VerbResult:
    return _restart_group(ctx, "agent", dry_run=dry_run)


# --------------------------------------------------------------------------
# native-kickstart / indesign-restart
# --------------------------------------------------------------------------

def native_kickstart(ctx: VerbContext, *, dry_run: bool, label: str) -> VerbResult:
    cmd = ["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"]
    before = {"label": label,
              "info": ((ctx.snapshot.get("native") or {}).get("agents") or {}).get(label)}
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "commands": [cmd]},
                          message=f"Would kickstart {label}.")
    outcome = _run_cmd(ctx, cmd, timeout=20)
    return VerbResult(ok=bool(outcome.get("ok")), before=before, result=outcome,
                      message=(f"Kickstarted {label}." if outcome.get("ok")
                              else f"Could not kickstart {label}; see result."))


def indesign_restart(ctx: VerbContext, *, dry_run: bool) -> VerbResult:
    """Quit InDesign over Apple Events, force-quit if that alone does not
    take, then kickstart the worker so it picks the relaunched app back up.
    Never touches a job folder — this only restarts the application and its
    login agent."""
    before = {"indesign": (ctx.snapshot.get("native") or {}).get("indesign")}
    quit_cmd = ["osascript", "-e",
               'tell application id "com.adobe.InDesign" to quit']
    kill_cmd = ["pkill", "-x", "Adobe InDesign 2025"]
    kick_cmd = ["launchctl", "kickstart", "-k",
               f"gui/{_uid()}/{_INDESIGN_WORKER_LABEL}"]
    commands = [quit_cmd, kill_cmd, kick_cmd]
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "commands": commands},
                          message="Would quit InDesign, force-kill if needed, and kickstart the worker.")
    quit_out = _run_cmd(ctx, quit_cmd, timeout=15)
    # A clean quit leaves nothing for pkill to find; that is success, not a
    # failure of this step, so its own ok/fail is not folded into the verb's.
    kill_out = _run_cmd(ctx, kill_cmd, timeout=10)
    kick_out = _run_cmd(ctx, kick_cmd, timeout=20)
    ok = bool(kick_out.get("ok"))
    return VerbResult(ok=ok, before=before,
                      result={"quit": quit_out, "pkill": kill_out, "kickstart": kick_out},
                      message=("Restarted InDesign and the worker." if ok
                              else "Could not kickstart the worker; see result."))


# --------------------------------------------------------------------------
# hubspot-fill-name / hubspot-set-name
# --------------------------------------------------------------------------

def _name_property_names(snapshot: dict) -> tuple[str, str]:
    watch = (snapshot.get("docwatch") or {}).get("watch") or {}
    return (watch.get("hubspot_first_property") or "author_first_name",
            watch.get("hubspot_last_property") or "author_last_name")


def _name_finding_for(ctx: VerbContext, project_id: str):
    from app.warden import names as nameslib

    for item in nameslib.compare(ctx.snapshot):
        if item.project_id == project_id:
            return item
    return None


def _write_hubspot_name(ctx: VerbContext, project_id: str, first: str, last: str,
                        *, dry_run: bool, before: dict) -> VerbResult:
    from app.watch import hubspot as hubspotlib

    first_prop, last_prop = _name_property_names(ctx.snapshot)
    props = {first_prop: first, last_prop: last}
    if dry_run:
        return VerbResult(ok=True, before=before,
                          result={"dry_run": True, "properties": props},
                          message=f"Would write {first} {last} onto Project {project_id}.")
    token = ctx.secrets.get("hubspot") if ctx.secrets else None
    if not token:
        return VerbResult(ok=False, before=before, message="no hubspot token configured.")
    try:
        hubspotlib.set_properties(token, _PROJECTS_OBJECT, project_id, props,
                                  allow={first_prop, last_prop}, opener=ctx.opener)
    except hubspotlib.HubSpotError as e:
        return VerbResult(ok=False, before=before, result={"error": str(e)},
                          message=f"HubSpot refused the write: {e}")
    return VerbResult(ok=True, before=before, result={"properties": props},
                      message=f"Wrote {first} {last} onto Project {project_id}.")


def hubspot_fill_name(ctx: VerbContext, *, dry_run: bool, project_id: str,
                      first: str, last: str) -> VerbResult:
    """Only for the safe case: HubSpot's name is blank and every other
    source already agrees. Recomputes the verdict itself rather than trusting
    the caller, so a stale request can never write a name HubSpot no longer
    actually disagrees about."""
    item = _name_finding_for(ctx, project_id)
    if item is None or item.verdict != "fill":
        return VerbResult(ok=False, message=(
            f"Project {project_id} is not a 'fill' case right now; refusing "
            f"to write its name."))
    before = {"hubspot": (item.evidence or {}).get("hubspot")}
    return _write_hubspot_name(ctx, project_id, first, last, dry_run=dry_run,
                               before=before)


def hubspot_set_name(ctx: VerbContext, *, dry_run: bool, project_id: str,
                     first: str, last: str) -> VerbResult:
    """The Tier-1 case: HubSpot disagrees with sources that agree with each
    other. Only reachable after a yes."""
    item = _name_finding_for(ctx, project_id)
    if item is None or item.verdict != "propose":
        return VerbResult(ok=False, message=(
            f"Project {project_id} is not a 'propose' case right now; "
            f"refusing to write its name."))
    before = {"hubspot": (item.evidence or {}).get("hubspot")}
    return _write_hubspot_name(ctx, project_id, first, last, dry_run=dry_run,
                               before=before)


# --------------------------------------------------------------------------
# registry / dispatch
# --------------------------------------------------------------------------

#: name -> (function, tier, needs_idle_agent)
REGISTRY: dict[str, tuple[Callable[..., VerbResult], int, bool]] = {
    "galley-nudge": (galley_nudge, 0, False),
    "galley-resume": (galley_resume, 0, False),
    "docwatch-requeue": (docwatch_requeue, 0, False),
    "docwatch-run": (docwatch_run, 0, False),
    "fly-restart-app": (fly_restart_app, 0, True),
    "fly-restart-agent": (fly_restart_agent, 1, True),
    "native-kickstart": (native_kickstart, 0, False),
    "indesign-restart": (indesign_restart, 0, False),
    "resend-completion": (resend_completion, 0, False),
    "hubspot-fill-name": (hubspot_fill_name, 0, False),
    "hubspot-set-name": (hubspot_set_name, 1, False),
}


def _journal_it(ctx: VerbContext, name: str, args: dict[str, Any], tier: int,
                dry_run: bool, result: VerbResult, finding_id: int | None) -> None:
    if ctx.journal is None:
        return
    try:
        ctx.journal.record_action(
            name, args, tier, dry_run, result.before,
            {"message": result.message, **result.result}, result.ok, finding_id)
    except Exception:                                          # noqa: BLE001
        log.warning("could not journal verb %s", name, exc_info=True)


def run_verb(ctx: VerbContext, name: str, args: dict[str, Any] | None,
            dry_run: bool, *, finding_id: int | None = None) -> VerbResult:
    """Look `name` up in `REGISTRY`, apply the claimed-book guard, call it,
    and journal the outcome — the one door every caller (the CLI's `verb`
    subcommand, the tick's Tier-0 loop, `listen()`'s "yes N") calls through,
    so nothing can run a verb without it being recorded."""
    call_args = dict(args or {})
    entry = REGISTRY.get(name)
    if entry is None:
        result = VerbResult(ok=False, message=f"no such verb: {name!r}")
        _journal_it(ctx, name, call_args, 0, dry_run, result, finding_id)
        return result

    fn, tier, needs_idle = entry
    if needs_idle and _agent_busy(ctx.snapshot):
        result = VerbResult(ok=False, message="a book is claimed")
        _journal_it(ctx, name, call_args, tier, dry_run, result, finding_id)
        return result

    try:
        result = fn(ctx, dry_run=dry_run, **call_args)
    except TypeError as e:
        result = VerbResult(ok=False, message=f"bad arguments for {name}: {e}")
    except Exception as e:                                     # noqa: BLE001
        log.warning("verb %s crashed", name, exc_info=True)
        result = VerbResult(ok=False, message=f"{name} raised: {e}")

    _journal_it(ctx, name, call_args, tier, dry_run, result, finding_id)
    return result


__all__ = ["VerbContext", "VerbResult", "REGISTRY", "run_verb"]
