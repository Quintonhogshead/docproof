"""The stuck rules: pure functions from one snapshot (plus recent history) to
a list of `Finding`s. No I/O, no model, nothing that can raise on a snapshot
that is missing a section — a source that failed to collect this tick is
just a rule that finds nothing to fire on, never a crash that takes the tick
down with it.

Each rule is named for the table in `docs/monitoring-agent-plan.md` ("Stuck
rules") and `WARDEN_BRIEF.md`; `evaluate()` runs all of them and never
raises. `history` (when given) is prior snapshots, OLDEST FIRST, not
including the current one — the same shape `journal`/`tick.py` would hand
back from its last N saved snapshots. Only `agent-stalled` and
`source-unreachable` use it; every other rule reads the current snapshot
alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("docproof.app.warden.rules")


@dataclass(frozen=True)
class Finding:
    rule: str
    key: str
    severity: str                      # "high" | "medium" | "low"
    summary: str
    evidence: dict = field(default_factory=dict)
    verbs: tuple[str, ...] = ()
    tier: int = 0                      # 0 auto, 1 ask, 2 human-only


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _snapshot_time(snapshot: dict, default: datetime | None) -> datetime | None:
    return _parse_dt(snapshot.get("at")) or default


def _docwatch(snapshot: dict) -> dict:
    section = snapshot.get("docwatch")
    return section if isinstance(section, dict) else {}


def _agent(snapshot: dict) -> dict:
    agent = _docwatch(snapshot).get("agent")
    return agent if isinstance(agent, dict) else {}


def _watch(snapshot: dict) -> dict:
    watch = _docwatch(snapshot).get("watch")
    return watch if isinstance(watch, dict) else {}


def _files(snapshot: dict) -> list:
    dw = _docwatch(snapshot)
    files = dw.get("files")
    if not isinstance(files, list):
        files = _watch(snapshot).get("files")
    return files if isinstance(files, list) else []


def _fly_group(snapshot: dict, group: str) -> dict:
    fly = snapshot.get("fly")
    if not isinstance(fly, dict):
        return {}
    section = fly.get(group)
    return section if isinstance(section, dict) else {}


# Default Galley claim spacing, mirroring `galley/agent.py::DEFAULT_BOOK_SPACING_S`
# (5 hours). The real value lives in the agent-group environment
# (`GALLEY_BOOK_SPACING_HOURS`) and is not itself part of the snapshot, so
# `book-unclaimed` uses this default plus `thresholds.unclaimed_extra_h`.
_DEFAULT_BOOK_SPACING_H = 5.0


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def agent_silent(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Newest Galley heartbeat older than 3x its own heartbeat_interval_s
    while a book is claimed."""
    agent = _agent(snapshot)
    if not agent:
        return []
    ledger = agent.get("ledger") or {}
    if not (ledger.get("claimed") or []):
        return []
    try:
        age = float(agent.get("age_s"))
        interval = float(agent.get("heartbeat_interval_s") or 60.0)
    except (TypeError, ValueError):
        return []
    factor = getattr(thresholds, "agent_silent_factor", 3)
    if age <= factor * interval:
        return []
    book = agent.get("book") or ""
    return [Finding(
        rule="agent-silent", key=str(book or "agent"), severity="high",
        summary=(f"Galley's heartbeat is {int(age)}s old (over {factor}x its "
                f"{int(interval)}s interval) while {book or 'a book'} is claimed."),
        evidence={"age_s": age, "heartbeat_interval_s": interval, "book": book},
        verbs=("galley-nudge",), tier=0)]


def agent_stalled(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Same phase and turn count across 90 minutes while state is running."""
    agent = _agent(snapshot)
    if agent.get("state") != "running":
        return []
    phase, turns = agent.get("phase"), agent.get("turns")
    if phase is None and turns is None:
        return []
    stable_since = _snapshot_time(snapshot, now)
    for prior in reversed(history or []):
        prior_agent = _agent(prior)
        if (prior_agent.get("state") != "running"
                or prior_agent.get("phase") != phase
                or prior_agent.get("turns") != turns):
            break
        stamp = _snapshot_time(prior, None)
        if stamp is None:
            break
        stable_since = stamp
    if stable_since is None:
        return []
    minutes = (_snapshot_time(snapshot, now) - stable_since).total_seconds() / 60.0
    threshold = getattr(thresholds, "agent_stalled_min", 90)
    if minutes < threshold:
        return []
    book = agent.get("book") or ""
    return [Finding(
        rule="agent-stalled", key=str(book or "agent"), severity="high",
        summary=(f"Galley has stayed on phase {phase!r} with {turns} turns "
                f"for {int(minutes)} minutes on {book or 'a book'}."),
        evidence={"phase": phase, "turns": turns, "minutes": minutes, "book": book},
        verbs=("galley-nudge",), tier=0)]


def agent_dead_token(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Heartbeat or log shows the 401 hold. Never fixable on its own."""
    agent = _agent(snapshot)
    reason = str(agent.get("credentials_error") or "")
    fly_logs = (snapshot.get("fly") or {}).get("logs", {}) if isinstance(snapshot.get("fly"), dict) else {}
    agent_logs = fly_logs.get("agent") or []
    log_hit = any("401" in line and ("token" in line.lower() or "credential" in line.lower())
                 for line in agent_logs)
    if not reason and not log_hit:
        return []
    return [Finding(
        rule="agent-dead-token", key=str(agent.get("book") or "agent"), severity="high",
        summary=f"Galley's subscription token is dead: {reason or 'a 401 appeared in the agent log'}.",
        evidence={"credentials_error": reason}, verbs=(), tier=2)]


def agent_quota_frozen(snapshot, thresholds, now, history=None) -> list[Finding]:
    """`.subscription-pause.json` present past its own resume time."""
    agent = _agent(snapshot)
    pause = agent.get("usage_pause")
    if not isinstance(pause, dict):
        return []
    try:
        resume_after = float(pause.get("resume_after"))
    except (TypeError, ValueError):
        return []
    if now.timestamp() < resume_after:
        return []
    return [Finding(
        rule="agent-quota-frozen", key=str(agent.get("book") or "agent"), severity="medium",
        summary=(f"The subscription pause's resume time has passed "
                f"({pause.get('reason', 'usage limit')})."),
        evidence={"resume_after": resume_after, "reason": pause.get("reason", "")},
        verbs=("galley-resume",), tier=0)]


def book_unclaimed(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A book has sat in awaiting longer than the claim spacing plus 1 hour
    with the agent idle."""
    dw = _docwatch(snapshot)
    agent = _agent(snapshot)
    if agent:
        ledger = agent.get("ledger") or {}
        if ledger.get("claimed") or agent.get("state") not in ("idle", "starting", "", None):
            return []
    extra_h = getattr(thresholds, "unclaimed_extra_h", 1)
    limit = timedelta(hours=_DEFAULT_BOOK_SPACING_H + extra_h)
    findings = []
    for book in dw.get("awaiting") or []:
        if not isinstance(book, dict):
            continue
        updated = _parse_dt(book.get("updated_at"))
        if updated is None or now - updated < limit:
            continue
        findings.append(Finding(
            rule="book-unclaimed", key=str(book.get("file_id") or book.get("name")),
            severity="medium",
            summary=(f"{book.get('name', 'A book')} has waited in awaiting since "
                    f"{book.get('updated_at')} with the agent idle."),
            evidence={"file_id": book.get("file_id"), "name": book.get("name"),
                      "updated_at": book.get("updated_at")},
            verbs=("docwatch-requeue",), tier=0))
    return findings


def book_abandoned(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Ledger says claimed, no heartbeat mentions it, machine restarted since."""
    agent = _agent(snapshot)
    ledger = agent.get("ledger") or {}
    claimed = ledger.get("claimed") or []
    if not claimed:
        return []
    active_book = agent.get("book") or ""
    restarted_at = None
    for machine in _fly_group(snapshot, "agent").get("machines") or []:
        stamp = _parse_dt(machine.get("updated_at"))
        if stamp and (restarted_at is None or stamp > restarted_at):
            restarted_at = stamp
    if restarted_at is None:
        return []
    findings = []
    for entry in claimed:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("file_id") or ""
        if active_book and active_book in (name, entry.get("file_id")):
            continue
        claim_time = _parse_dt(entry.get("updated_at"))
        if claim_time is None or restarted_at <= claim_time:
            continue
        findings.append(Finding(
            rule="book-abandoned", key=str(entry.get("file_id") or name), severity="high",
            summary=(f"{name} is claimed in the ledger but the agent's heartbeat no "
                    f"longer mentions it, and the agent machine restarted since."),
            evidence={"file_id": entry.get("file_id"), "name": name,
                      "updated_at": entry.get("updated_at"),
                      "machine_restarted_at": restarted_at.isoformat()},
            verbs=("galley-nudge",), tier=0))
    return findings


def watch_tick_missed(snapshot, thresholds, now, history=None) -> list[Finding]:
    """DocWatch's last tick is later than the schedule allows, or the
    schedule's fixed times passed with no pass."""
    dw = _docwatch(snapshot)
    watch = _watch(snapshot)
    if not watch.get("auto_ticks"):
        return []
    last_tick_at = dw.get("last_tick") or watch.get("last_tick_at")
    stamp = _parse_dt(last_tick_at)
    factor = getattr(thresholds, "tick_late_factor", 2)
    interval_min = watch.get("tick_every_minutes") or 60
    limit = timedelta(minutes=interval_min * factor)

    late, detail = False, ""
    if stamp is None or now - stamp >= limit:
        late = True
        detail = f"no tick within {interval_min * factor} minutes"
    next_tick_at = watch.get("next_tick_at")
    nxt = _parse_dt(next_tick_at)
    if nxt and now >= nxt and (stamp is None or stamp < nxt):
        late = True
        detail = f"the scheduled tick at {next_tick_at} has passed with no pass since"
    if not late:
        return []
    return [Finding(
        rule="watch-tick-missed", key="watch", severity="high",
        summary=f"DocWatch's tick is late: {detail}.",
        evidence={"last_tick_at": last_tick_at, "next_tick_at": next_tick_at,
                  "tick_every_minutes": interval_min},
        verbs=("docwatch-run", "fly-restart-app"), tier=0)]


def watch_signin_dead(snapshot, thresholds, now, history=None) -> list[Finding]:
    """The sign-in preflight fails or the log shows the dead-sign-in message."""
    dw = _docwatch(snapshot)
    sign_in = dw.get("sign_in")
    watch = _watch(snapshot)
    dead, message = False, ""
    if isinstance(sign_in, dict) and sign_in.get("state") == "failed":
        dead, message = True, str(sign_in.get("message", ""))
    if watch.get("missing") == "auth":
        dead = True
        message = message or "DocWatch reports it has no working Google sign-in."
    if not dead:
        return []
    summary = (f"DocWatch's Google sign-in is dead: {message}" if message
              else "DocWatch's Google sign-in is dead.")
    return [Finding(
        rule="watch-signin-dead", key="docwatch", severity="high", summary=summary,
        evidence={"sign_in": sign_in, "missing": watch.get("missing")},
        verbs=(), tier=2)]


def watch_model_unavailable(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A pass ended in ModelUnavailable."""
    dw = _docwatch(snapshot)
    last_pass = dw.get("last_pass")
    if not isinstance(last_pass, dict):
        last_pass = _watch(snapshot).get("last_pass")
    if not isinstance(last_pass, dict):
        return []
    note = str(last_pass.get("note") or last_pass.get("error")
              or last_pass.get("outcome") or "")
    if "modelunavailable" not in note.lower().replace(" ", ""):
        return []
    return [Finding(
        rule="watch-model-unavailable", key="docwatch", severity="medium",
        summary=f"The last DocWatch pass ended in ModelUnavailable: {note}",
        evidence={"last_pass": last_pass}, verbs=(), tier=1)]


def watch_file_skipped_silently(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A file the watcher has seen but never resolved, sitting unread past
    `skipped_ticks` ticks (the Morales `.doc` case).

    The snapshot carries no raw Drive folder listing, only DocProof's own
    file records, so this can only catch a file that got as far as a record
    with an error and then went quiet — not one that never became a record
    at all. See the deviation note in the build report."""
    files = _files(snapshot)
    skipped_ticks = getattr(thresholds, "skipped_ticks", 2)
    tick_minutes = _watch(snapshot).get("tick_every_minutes") or 60
    limit = timedelta(minutes=tick_minutes * skipped_ticks)
    findings = []
    for rec in files:
        if not isinstance(rec, dict):
            continue
        if rec.get("marked") or rec.get("job_id") or rec.get("done"):
            continue
        error = rec.get("error")
        if not error:
            continue
        updated = _parse_dt(rec.get("updated_at"))
        if updated is None or now - updated < limit:
            continue
        findings.append(Finding(
            rule="watch-file-skipped-silently",
            key=str(rec.get("file_id") or rec.get("name")), severity="medium",
            summary=(f"{rec.get('name', 'A file')} has sat unread for over "
                    f"{skipped_ticks} ticks: {error}"),
            evidence={"file_id": rec.get("file_id"), "name": rec.get("name"),
                      "error": error, "updated_at": rec.get("updated_at")},
            verbs=(), tier=1))
    return findings


def native_worker_down(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Either login agent not running, or InDesign fails the liveness probe."""
    native = snapshot.get("native")
    if not isinstance(native, dict) or not native.get("installed"):
        return []
    findings = []
    for label, info in (native.get("agents") or {}).items():
        if isinstance(info, dict) and not info.get("running"):
            findings.append(Finding(
                rule="native-worker-down", key=str(label), severity="high",
                summary=f"{label} is not running on the Mini.",
                evidence={"label": label, "info": info},
                verbs=("native-kickstart",), tier=0))
    indesign = native.get("indesign") or {}
    if not indesign.get("alive"):
        findings.append(Finding(
            rule="native-worker-down", key="indesign", severity="high",
            summary=(f"InDesign did not answer the liveness probe: "
                    f"{indesign.get('error') or 'no response'}"),
            evidence={"indesign": indesign}, verbs=("native-kickstart",), tier=0))
    return findings


def native_batch_held(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A batch in `held`, or an unresolved Project older than 24 hours."""
    native = snapshot.get("native")
    if not isinstance(native, dict):
        return []
    findings = []
    for batch in native.get("batches") or []:
        if not isinstance(batch, dict) or batch.get("state") != "held":
            continue
        findings.append(Finding(
            rule="native-batch-held", key=str(batch.get("id")), severity="medium",
            summary=(f"Batch {batch.get('id')} for {batch.get('book', 'a book')} "
                    f"is held: {batch.get('hold_reason') or 'no reason given'}."),
            evidence={"batch": batch}, verbs=(), tier=2))
    return findings


_OOM_RE = re.compile(r"\boom\b|out of memory", re.IGNORECASE)


def machine_oom(snapshot, thresholds, now, history=None) -> list[Finding]:
    """Fly log shows an OOM kill on either group."""
    fly = snapshot.get("fly")
    if not isinstance(fly, dict):
        return []
    logs = fly.get("logs") or {}
    findings = []
    for group, verb in (("app", "fly-restart-app"), ("agent", "fly-restart-agent")):
        for line in logs.get(group) or []:
            if _OOM_RE.search(line):
                findings.append(Finding(
                    rule="machine-oom", key=group, severity="medium",
                    summary=f"The {group} machine's log shows an out-of-memory kill.",
                    evidence={"group": group, "line": line}, verbs=(verb,), tier=1))
                break
    return findings


def deploy_during_run(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A release landed on the agent group while a book was claimed."""
    agent = _agent(snapshot)
    ledger = agent.get("ledger") or {}
    claimed = ledger.get("claimed") or []
    if not claimed:
        return []
    release = _fly_group(snapshot, "agent").get("release") or {}
    deployed_at = _parse_dt(release.get("created_at"))
    if deployed_at is None:
        return []
    findings = []
    for entry in claimed:
        if not isinstance(entry, dict):
            continue
        claimed_at = _parse_dt(entry.get("updated_at"))
        if claimed_at is None or deployed_at <= claimed_at:
            continue
        findings.append(Finding(
            rule="deploy-during-run", key=str(entry.get("file_id") or entry.get("name")),
            severity="high",
            summary=(f"A release (v{release.get('version')}) landed on the agent "
                    f"group at {release.get('created_at')} while "
                    f"{entry.get('name')} was claimed."),
            evidence={"release": release, "claim": entry}, verbs=(), tier=2))
    return findings


_SOURCES = ("fly", "docwatch", "hubspot", "native", "email")


def source_unreachable(snapshot, thresholds, now, history=None) -> list[Finding]:
    """A snapshot section carries `error` for 3 consecutive snapshots."""
    recent = list(history or [])[-2:] + [snapshot]
    if len(recent) < 3:
        return []
    findings = []
    for source in _SOURCES:
        errors = []
        for snap in recent:
            section = snap.get(source)
            err = section.get("error") if isinstance(section, dict) else None
            if not err:
                errors = []
                break
            errors.append(err)
        if len(errors) == len(recent):
            findings.append(Finding(
                rule="source-unreachable", key=source, severity="high",
                summary=f"{source} has failed for {len(recent)} consecutive snapshots: {errors[-1]}",
                evidence={"source": source, "errors": errors}, verbs=(), tier=2))
    return findings


_RULES = (
    agent_silent, agent_stalled, agent_dead_token, agent_quota_frozen,
    book_unclaimed, book_abandoned, watch_tick_missed, watch_signin_dead,
    watch_model_unavailable, watch_file_skipped_silently, native_worker_down,
    native_batch_held, machine_oom, deploy_during_run, source_unreachable,
)


def evaluate(snapshot: dict, thresholds, *, now: datetime,
            history: list[dict] | None = None) -> list[Finding]:
    """Run every rule against `snapshot`. Never raises — a rule that cannot
    make sense of what it was given simply finds nothing."""
    findings: list[Finding] = []
    for rule_fn in _RULES:
        try:
            findings.extend(rule_fn(snapshot, thresholds, now, history=history) or [])
        except Exception:                                       # noqa: BLE001
            log.warning("rule %s crashed", rule_fn.__name__, exc_info=True)
    return findings
