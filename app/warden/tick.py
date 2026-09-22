"""The two clocks: a full tick every `tick_interval_min`, and a cheap
`listen()` every `listen_interval_min` that only reads inbound messages.

`run_tick` is the whole deterministic cycle from `docs/monitoring-agent-plan.md`:
snapshot, evaluate the stuck rules plus the names lane, reconcile against the
journal, take at most one Tier-0 fix, notify about what's new or resolved,
turn Tier-1 findings into numbered approval requests, expire stale requests,
write `tick/latest.json` for both a person and the harness to read, and wake
the harness when something needs judgement a rule can't supply. Every step is
individually small and re-runs cleanly — a tick that dies partway through
loses nothing the *next* tick can't rediscover from the journal and a fresh
snapshot.

`listen()` is the other half of "talking to people": it reads whatever came
in since the last listen (an iMessage from the owner's number, a reply in the
notify mailbox), routes it through `commands.handle`, executes whatever
action that implies, and replies. It never runs a Tier-0 fix on its own
initiative — only what `commands.handle` or a "yes N" explicitly asked for."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.warden import approvals, commands, harness, names, rules, snapshot as snapshot_module, verbs
from app.warden import config as config_module
from app.warden.messaging import imessage

log = logging.getLogger("docproof.app.warden.tick")

#: How long a Tier-0 fix stays "already tried" before the same finding may be
#: acted on again.
_RETRY_AFTER = timedelta(hours=6)

#: How many prior snapshots `agent-stalled` and `source-unreachable` get to
#: look back across.
_HISTORY_DEPTH = 3
#: How long the same finding stays out of `needs_model` after the harness
#: has looked at it once. A dead token or a held batch is the same problem
#: at 14:00 as at 13:40; waking the model for it every tick spends the
#: subscription and says nothing new.
_MODEL_RECONSULT_AFTER = timedelta(hours=6)
_MODEL_SEEN_KEY = "model_seen:{}"
_CODE_STARTED_KEY = "code_started:{}"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class TickResult:
    snapshot: dict
    snapshot_path: Path | None
    findings: list = field(default_factory=list)
    action: dict | None = None
    requests: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    needs_model: list = field(default_factory=list)
    harness_output: str | None = None
    tick_path: Path | None = None


@dataclass
class ListenResult:
    handled: list = field(default_factory=list)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _home(home: str | Path | None) -> Path:
    return Path(home) if home is not None else config_module.home()


def _load_history(home: Path, *, before: str | None, depth: int) -> list[dict]:
    """The last `depth` saved snapshots, oldest first, strictly older than
    `before` (the timestamp of the snapshot just taken) — what `rules.evaluate`
    calls `history`."""
    snap_dir = home / "snapshots"
    if not snap_dir.is_dir():
        return []
    files = sorted(p for p in snap_dir.glob("*.json") if p.name != "latest.json")
    out: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if before and str(data.get("at") or "") >= before:
            continue
        out.append(data)
    return out[-depth:]


def _finding_key(f) -> tuple[str, str]:
    return (f.rule, f.key)


def _pick_verb(finding) -> str | None:
    """`native-worker-down` fires with the same verb suggestion
    (`native-kickstart`) whether a login agent died or InDesign itself
    stopped answering — but InDesign isn't a launchd label, so the
    `indesign` key is routed to `indesign-restart` here instead of trusting
    `rules.py`'s generic suggestion literally."""
    if finding.rule == "native-worker-down" and finding.key == "indesign":
        return "indesign-restart"
    return finding.verbs[0] if finding.verbs else None


def _verb_args(finding, *, name_index: dict[str, Any]) -> dict[str, Any] | None:
    """Build the arguments a Tier-0/Tier-1 auto-fire needs for `finding`, or
    `None` when this finding can't be turned into a verb call automatically
    (it then falls through to `needs_model`)."""
    ev = finding.evidence or {}
    verb = _pick_verb(finding)
    if verb is None:
        return None

    if verb == "galley-nudge":
        book = ev.get("book") or ev.get("name") or finding.key
        return {"book": book} if book else None
    if verb in ("galley-resume", "docwatch-run", "fly-restart-app",
               "fly-restart-agent", "indesign-restart"):
        return {}
    if verb == "docwatch-requeue":
        file_id = ev.get("file_id") or finding.key
        return {"file_id": file_id} if file_id else None
    if verb == "native-kickstart":
        label = ev.get("label") or finding.key
        return {"label": label} if label else None
    if verb in ("hubspot-fill-name", "hubspot-set-name"):
        item = name_index.get(finding.key)
        if item is None or not item.proposal:
            return None
        return {"project_id": finding.key, "first": item.proposal.get("first", ""),
                "last": item.proposal.get("last", "")}
    return None


def _in_quiet_hours(config, journal, now: datetime) -> bool:
    quiet_until = _parse(journal.get("quiet_until"))
    if quiet_until and now < quiet_until:
        return True
    tz_name = getattr(config, "timezone", "America/New_York") or "America/New_York"
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:                                          # noqa: BLE001
        local = now
    start_h, start_m = _hhmm(getattr(config, "quiet_start", "23:00"))
    end_h, end_m = _hhmm(getattr(config, "quiet_end", "07:00"))
    cur = local.hour * 60 + local.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def _hhmm(text: str) -> tuple[int, int]:
    try:
        h, m = str(text or "00:00").split(":")
        return int(h), int(m)
    except (ValueError, TypeError):
        return 0, 0


def _text_owner(config, journal, run, text: str, *, now: datetime, high: bool = False) -> bool:
    """Send one text to `config.owner_handle`, respecting quiet hours
    (unless `high`) and the hourly cap, and journal it on success. `False`
    means nothing went out — quiet hours, the cap, iMessage disabled, no
    handle configured, or `osascript` itself refusing."""
    if not getattr(config, "imessage_enabled", True) or not getattr(config, "owner_handle", ""):
        return False
    if high is not True and _in_quiet_hours(config, journal, now):
        return False
    sent = imessage.send(config.owner_handle, text, run=run, journal=journal,
                         max_per_hour=getattr(config, "max_texts_per_hour", 4))
    if sent:
        journal.record_message("imessage", "out", config.owner_handle, text)
    return sent


# --------------------------------------------------------------------------
# run_tick
# --------------------------------------------------------------------------

def run_tick(config, *, secrets, journal, run, opener, now: datetime,
            home: str | Path | None = None, dry_run: bool = False,
            allow_model: bool = True) -> TickResult:
    home_path = _home(home)

    # 1. snapshot -----------------------------------------------------------
    snap = snapshot_module.collect(config, secrets=secrets, run=run, opener=opener,
                                   now=now, journal=journal)
    snap_path = snapshot_module.save(home_path, snap)
    history = _load_history(home_path, before=str(snap.get("at") or ""),
                            depth=_HISTORY_DEPTH)

    # 2. evaluate -------------------------------------------------------
    findings = list(rules.evaluate(snap, config.thresholds, now=now, history=history))
    name_findings = names.compare(snap)
    name_index = {nf.project_id: nf for nf in name_findings}
    findings += names.as_findings(name_findings)

    # 3. journal upsert/resolve ---------------------------------------------
    seen = {_finding_key(f) for f in findings}
    resolved = journal.resolve_missing(seen)
    journal_findings: dict[tuple[str, str], Any] = {}
    for f in findings:
        journal_findings[_finding_key(f)] = journal.upsert_finding(
            f.rule, f.key, f.severity, f.summary, f.evidence)

    paused = journal.get("paused") in ("1", "true", "True")

    # 4. one Tier-0 action ----------------------------------------------
    action_taken: dict[str, Any] | None = None
    if not paused:
        for f in findings:
            if f.tier != 0:
                continue
            jf = journal_findings[_finding_key(f)]
            verb_name = _pick_verb(f)
            if verb_name is None:
                continue
            last = journal.last_action(verb_name, key=jf.id)
            if last is not None:
                last_at = _parse(last.at)
                if last_at is not None and now - last_at < _RETRY_AFTER:
                    continue
            args = _verb_args(f, name_index=name_index)
            if args is None:
                continue
            ctx = verbs.VerbContext(config=config, secrets=secrets, journal=journal,
                                    snapshot=snap, run=run, opener=opener, now=now)
            result = verbs.run_verb(ctx, verb_name, args, dry_run, finding_id=jf.id)
            action_taken = {"verb": verb_name, "args": args, "finding_id": jf.id,
                            "ok": result.ok, "message": result.message}
            break

    if action_taken is not None and not dry_run:
        # Re-snapshot so the next tick (and this one's own record) sees the
        # state the action actually left behind, not the state that
        # triggered it.
        snap = snapshot_module.collect(config, secrets=secrets, run=run,
                                       opener=opener, now=now, journal=journal)
        snap_path = snapshot_module.save(home_path, snap)

    # 5. notify: new findings, then one text for whatever just resolved -----
    for f in findings:
        if f.severity not in ("high", "medium"):
            continue
        if f.tier == 1 and f.verbs:
            continue  # goes through the Tier-1 request text instead (step 6)
        jf = journal_findings[_finding_key(f)]
        if jf.notified_at:
            continue
        text = f"[{f.severity.title()}] {f.summary}"
        if _text_owner(config, journal, run, text, now=now, high=f.severity == "high"):
            journal.mark_notified(jf.id)

    if resolved:
        text = "Resolved: " + "; ".join(r.summary for r in resolved[:5])
        if len(resolved) > 5:
            text += f" (+{len(resolved) - 5} more)"
        _text_owner(config, journal, run, text, now=now, high=False)

    # 6. expire stale requests first, so a request whose own deadline has
    # just passed does not block a fresh ask for the same finding below.
    # (The brief orders "Tier-1 requests" before "expire"; swapped here on
    # purpose — see the build report.)
    journal.expire_requests(now, default_hours=getattr(config.thresholds, "request_expiry_h", 12))

    # 7. Tier-1 findings -> numbered requests --------------------------------
    open_by_finding = {
        req.payload.get("finding_id"): req for req in journal.open_requests()}
    request_summaries: list[dict[str, Any]] = []
    for f in findings:
        if f.tier != 1 or not f.verbs:
            continue
        jf = journal_findings[_finding_key(f)]
        if jf.id in open_by_finding:
            continue
        verb_name = _pick_verb(f)
        args = _verb_args(f, name_index=name_index) if verb_name else None
        if verb_name is None or args is None:
            continue
        expires_at = now + timedelta(hours=getattr(config.thresholds, "request_expiry_h", 12))
        req = journal.open_request(
            "action", f.summary, {"verb": verb_name, "args": args, "finding_id": jf.id},
            expires_at=expires_at)
        _text_owner(config, journal, run, approvals.request_text(req), now=now,
                   high=f.severity == "high")
        journal.mark_notified(jf.id)

    for req in journal.open_requests():
        request_summaries.append({"number": req.number, "text": req.text,
                                  "status": req.status, "expires_at": req.expires_at})

    # 8. needs_model + tick/latest.json --------------------------------------
    # A finding "needs the model" when the deterministic layer has no verb
    # for it at all, or has a verb but couldn't build its arguments (an
    # "unknown cause") — NOT merely because this particular tick's one-
    # action budget went to a different finding first; that one simply
    # fires on a later tick, and treating it as needs_model here would wake
    # the harness for something already fully automated.
    needs_model: list[dict[str, Any]] = []
    for f in findings:
        verb_name = _pick_verb(f)
        args = _verb_args(f, name_index=name_index) if verb_name else None
        if verb_name is not None and args is not None:
            continue
        fid = journal_findings[_finding_key(f)].id
        seen = _parse(journal.get(_MODEL_SEEN_KEY.format(fid)))
        if seen is not None and now - seen < _MODEL_RECONSULT_AFTER:
            continue
        needs_model.append({"rule": f.rule, "key": f.key, "severity": f.severity,
                            "summary": f.summary, "tier": f.tier, "id": fid})

    tick_payload = {
        "at": _iso(now),
        "snapshot": str(snap_path) if snap_path else None,
        "findings": [{"rule": f.rule, "key": f.key, "severity": f.severity,
                      "summary": f.summary, "tier": f.tier, "verbs": list(f.verbs)}
                     for f in findings],
        "action": action_taken,
        "requests": request_summaries,
        "resolved": [{"rule": r.rule, "key": r.key, "summary": r.summary} for r in resolved],
        "needs_model": needs_model,
    }
    tick_path = home_path / "tick" / "latest.json"
    tick_path.parent.mkdir(parents=True, exist_ok=True)
    tick_path.write_text(json.dumps(tick_payload, indent=2, ensure_ascii=False), "utf-8")

    journal.set("last_tick_at", _iso(now))

    # 9. wake the harness when something needs judgement --------------------
    harness_output = None
    model_on = allow_model and getattr(config, "harness", "claude") != "none"
    approved = _approved_code_request(journal) if model_on else None
    if approved is not None:
        # An approved code request outranks this tick's findings: the person
        # said yes, and the PR is the one thing only the model can produce.
        journal.set(_CODE_STARTED_KEY.format(approved.number), _iso(now))
        harness_output = harness.run(config, home_path, mode="code",
                                     question=json.dumps(approved.payload), run=run)
        if harness_output:
            journal.record_message("harness", "out", "warden", harness_output,
                                   ref=f"code-request:{approved.number}")
    elif needs_model and model_on:
        for item in needs_model:
            journal.set(_MODEL_SEEN_KEY.format(item["id"]), _iso(now))
        harness_output = harness.run(config, home_path, mode="tick", run=run)
        if harness_output:
            journal.record_message("harness", "out", "warden", harness_output)

    return TickResult(snapshot=snap, snapshot_path=snap_path, findings=findings,
                      action=action_taken, requests=request_summaries,
                      resolved=list(resolved), needs_model=needs_model,
                      harness_output=harness_output, tick_path=tick_path)


# --------------------------------------------------------------------------
# listen()
# --------------------------------------------------------------------------

def _approved_code_request(journal):
    """The one `code` request answered yes that no harness run has started."""
    for req in journal.answered_requests("yes"):
        if req.kind == "code" and not journal.get(_CODE_STARTED_KEY.format(req.number)):
            return req
    return None


def _load_latest_snapshot(home: Path) -> dict:
    path = home / "snapshots" / "latest.json"
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _run_request(config, secrets, journal, snapshot, run, opener, now, req) -> str:
    verb_name = req.payload.get("verb")
    if not verb_name:
        return "Noted — there's no automated fix for this one."
    ctx = verbs.VerbContext(config=config, secrets=secrets, journal=journal,
                            snapshot=snapshot, run=run, opener=opener, now=now)
    result = verbs.run_verb(ctx, verb_name, req.payload.get("args") or {}, False,
                            finding_id=req.payload.get("finding_id"))
    return result.message


def _send_reply(channel: str, party: str, text: str, reply_text: str, *,
                config, secrets, journal, run, opener, now) -> dict[str, Any]:
    """Send `reply_text` back over whichever channel `text` arrived on, and
    return the row `listen()` records in its result. iMessage replies
    respect the rate cap (via `_text_owner`, always `high=True` so a direct
    reply is never itself swallowed by quiet hours); an email reply goes out
    through the team address, best-effort."""
    if channel == "imessage":
        _text_owner(config, journal, run, reply_text, now=now, high=True)
    elif channel == "email":
        try:
            from app.warden.messaging import gmail
            gmail.send_team(config, secrets, "Re: your message", reply_text, opener=opener)
            journal.record_message("email", "out", party, reply_text)
        except Exception:                                       # noqa: BLE001
            log.warning("could not send an email reply", exc_info=True)
    return {"channel": channel, "party": party, "text": text, "reply": reply_text}


def _handle_inbound(text: str, *, channel: str, party: str, config, secrets,
                    journal, snapshot, now, run, opener, home, can_act: bool) -> dict[str, Any]:
    # `commands.handle` applies a "yes"/"no" the instant it sees one
    # (`approvals.apply_answer` mutates the request's status) — it has no
    # notion of which channel asked. So an email's approval-shaped text must
    # be refused BEFORE `commands.handle` ever runs, not after: only a text
    # from `can_act`'s iMessage channel may reach it at all when the text
    # parses as an answer. Every other command (status, why, an unmatched
    # question) is harmless to let email ask, so those still go through.
    if not can_act and approvals.parse_answer(text) is not None:
        reply_text = ("I can't approve anything from email — only a text "
                      "from the owner's own number can say yes or no.")
        return _send_reply(channel, party, text, reply_text, config=config,
                           secrets=secrets, journal=journal, run=run, opener=opener, now=now)

    reply = commands.handle(text, journal=journal, config=config,
                            snapshot=snapshot, now=now)
    reply_text = reply.text
    action = reply.action

    if action is None:
        pass
    elif action[0] == "model":
        question = action[1]
        reply_text = harness.run(config, home, mode="question", question=question, run=run)
        journal.record_message("harness", "out", "warden", reply_text)
    elif not can_act:
        reply_text = (f"{reply_text} (I only act on 'yes'/'no', 'forget', or "
                      f"other commands from the owner's own number, not from "
                      f"email.)").strip()
    elif action[0] == "run_request":
        number = action[1]
        req = journal.get_request(number)
        if req is not None:
            outcome = _run_request(config, secrets, journal, snapshot, run, opener, now, req)
            reply_text = f"{reply_text} {outcome}".strip()
    elif action[0] == "verb":
        _, verb_name, args = action
        ctx = verbs.VerbContext(config=config, secrets=secrets, journal=journal,
                                snapshot=snapshot, run=run, opener=opener, now=now)
        result = verbs.run_verb(ctx, verb_name, args, False)
        reply_text = f"{reply_text} {result.message}".strip()

    return _send_reply(channel, party, text, reply_text, config=config,
                       secrets=secrets, journal=journal, run=run, opener=opener, now=now)


def listen(config, *, secrets, journal, run, opener, now: datetime,
          home: str | Path | None = None) -> ListenResult:
    home_path = _home(home)
    snapshot = _load_latest_snapshot(home_path)
    handled: list[dict[str, Any]] = []

    if getattr(config, "imessage_enabled", True) and getattr(config, "owner_handle", ""):
        since = _parse(journal.get("last_imessage_at")) or (now - timedelta(hours=24))
        try:
            inbound = imessage.read_since(since, allowed_handles=[config.owner_handle])
        except Exception:                                       # noqa: BLE001
            log.warning("could not read chat.db", exc_info=True)
            inbound = []
        for msg in inbound:
            journal.record_message("imessage", "in", msg.handle, msg.text)
            handled.append(_handle_inbound(
                msg.text, channel="imessage", party=msg.handle, config=config,
                secrets=secrets, journal=journal, snapshot=snapshot, now=now,
                run=run, opener=opener, home=home_path, can_act=True))
            journal.set("last_imessage_at", _iso(msg.at))

    try:
        from app.warden.messaging import gmail
        since_email = _parse(journal.get("last_email_cursor")) or (now - timedelta(hours=24))
        replies = gmail.replies(config, secrets, since=since_email, opener=opener)
    except Exception:                                           # noqa: BLE001
        replies = []
    for msg in replies:
        journal.record_message("email", "in", msg.get("from", ""),
                               msg.get("snippet", ""), ref=msg.get("id", ""))
        handled.append(_handle_inbound(
            msg.get("snippet", ""), channel="email", party=msg.get("from", ""),
            config=config, secrets=secrets, journal=journal, snapshot=snapshot,
            now=now, run=run, opener=opener, home=home_path, can_act=False))
    if replies:
        journal.set("last_email_cursor", _iso(now))

    journal.set("last_listen_at", _iso(now))
    return ListenResult(handled=handled)


#: Public aliases for `cli.py`'s `say` command, which needs the same
#: quiet-hours/rate-cap logic (and the reasons a send was refused) without
#: duplicating it.
in_quiet_hours = _in_quiet_hours
text_owner = _text_owner

__all__ = ["TickResult", "ListenResult", "run_tick", "listen",
          "in_quiet_hours", "text_owner"]
