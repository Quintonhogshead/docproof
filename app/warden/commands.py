"""The deterministic half of "talking to people": everything that answers
without waking the model.

`docs/monitoring-agent-plan.md`'s inbound vocabulary — `status`, `yes N`,
`no N`, `pause`, `resume`, `quiet 3h`, `forget <surname>`, `why <surname>` —
is small and fixed on purpose: a person on a phone typing a command should
get an instant, predictable reply, and the model tick should only ever see
the things nobody anticipated. `handle()` is pure dispatch over that
vocabulary; it never calls a verb itself. Where a command implies doing
something beyond the journal/kv bookkeeping it can do alone (running a
request's verb, nudging Galley), it hands the caller a `(verb, name, args)`
or `(kind, ...)` tuple in `Reply.action` and stops — `tick.py`/`listen()`
(Agent E) is what actually calls into `verbs.py`, after whatever additional
checks that layer wants (the claimed-book guard, dry-run, etc).

Anything that does not match the vocabulary is not an error: it is a
question for the harness, returned as `action=("model", text)` with no reply
text of its own, exactly like an unrecognised finding falls through to
`needs_model` in the tick.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.warden import approvals
from app.warden.config import WardenConfig
from app.warden.journal import Journal

log = logging.getLogger("docproof.app.warden.commands")

EASTERN = ZoneInfo("America/New_York")

_QUIET_RE = re.compile(
    r"^quiet\s+(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\s*$",
    re.IGNORECASE)
_FORGET_RE = re.compile(r"^forget\s+(.+)$", re.IGNORECASE)
_WHY_RE = re.compile(r"^why\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Reply:
    """What to send back, and what the caller should do about it, if
    anything. `action` is one of:

    - `None` — nothing further to do; `text` is the whole answer.
    - `("run_request", number)` — a `yes N` was applied; run that request's
      verb.
    - `("verb", name, args)` — run this verb directly (`forget <surname>`).
    - `("model", question)` — unrecognised text; wake the harness with it.
    """

    text: str
    action: tuple[Any, ...] | None = None


def handle(text: str, *, journal: Journal, config: WardenConfig,
           snapshot: dict[str, Any], now: datetime) -> Reply:
    raw = (text or "").strip()
    lowered = raw.lower()

    if lowered == "status":
        return Reply(text=_status_text(snapshot, journal, now))

    if approvals.parse_answer(raw) is not None:
        return _handle_yes_no(raw, journal=journal, config=config, now=now)

    if lowered == "pause":
        journal.set("paused", "1")
        return Reply(text="Paused. I'll keep watching but won't act on "
                          "anything until you say 'resume'.")

    if lowered == "resume":
        journal.set("paused", "0")
        return Reply(text="Resumed. Acting on findings again.")

    qm = _QUIET_RE.match(raw)
    if qm:
        return _handle_quiet(qm, journal=journal, now=now)

    fm = _FORGET_RE.match(raw)
    if fm:
        surname = fm.group(1).strip()
        return Reply(
            text=f"On it — nudging Galley to forget {surname}'s run.",
            action=("verb", "galley-nudge", {"book": surname}))

    wm = _WHY_RE.match(raw)
    if wm:
        surname = wm.group(1).strip()
        return Reply(text=_why_text(surname, journal=journal,
                                     snapshot=snapshot, now=now))

    return Reply(text="", action=("model", raw))


# -- yes/no -----------------------------------------------------------------

def _handle_yes_no(raw: str, *, journal: Journal, config: WardenConfig,
                    now: datetime) -> Reply:
    parsed = approvals.parse_answer(raw)
    assert parsed is not None
    answer, number = parsed
    req = approvals.apply_answer(journal, raw, config.owner_name, now)
    if req is None:
        return Reply(text=f"#{number} isn't open anymore (already "
                          "answered, expired, or doesn't exist).")
    if answer == "yes":
        return Reply(text=f"Yes on #{number}. Running it now.",
                     action=("run_request", number))
    return Reply(text=f"No on #{number}. Leaving it alone.")


# -- quiet --------------------------------------------------------------

def _handle_quiet(match: re.Match, *, journal: Journal, now: datetime
                   ) -> Reply:
    amount = int(match.group(1))
    unit = match.group(2).lower()
    delta = timedelta(hours=amount) if unit.startswith("h") \
        else timedelta(minutes=amount)
    until = now + delta
    journal.set("quiet_until", until.isoformat())
    return Reply(text=f"Quiet until {_fmt_eastern(until)}.")


# -- status ---------------------------------------------------------------

def _status_text(snapshot: dict[str, Any], journal: Journal, now: datetime
                  ) -> str:
    docwatch = snapshot.get("docwatch") or {}
    agent = docwatch.get("agent") or {}
    state = agent.get("state") or "unknown"
    book = agent.get("book") or "no book"
    phase = agent.get("phase") or "-"
    age_s = agent.get("age_s")
    age_txt = _fmt_age(age_s) if age_s is not None else "unknown age"

    awaiting = docwatch.get("awaiting") or []
    last_tick = docwatch.get("last_tick")
    last_tick_txt = _fmt_iso_eastern(last_tick) if last_tick else "never"

    open_findings = journal.open_findings()
    open_requests = journal.open_requests()
    paused = journal.get("paused") in ("1", "true", "True")

    return (
        f"Galley is {state} on {book} (phase {phase}, {age_txt})."
        f"{' Paused.' if paused else ''} "
        f"{len(awaiting)} book(s) awaiting. "
        f"DocWatch last ticked {last_tick_txt}. "
        f"{len(open_findings)} open finding(s), "
        f"{len(open_requests)} open request(s)."
    )


# -- why --------------------------------------------------------------------

def _why_text(surname: str, *, journal: Journal, snapshot: dict[str, Any],
              now: datetime) -> str:
    needle = surname.strip().lower()
    if not needle:
        return "Whose name?"

    findings = [f for f in journal.open_findings()
                if _mentions(needle, f.key, f.summary, f.evidence)]
    actions = [a for a in journal.recent_actions(hours=48)
               if _mentions(needle, a.verb, a.args, a.before, a.result)]
    files = _matching_files(snapshot, needle)

    parts: list[str] = []
    if findings:
        parts.append(
            f"{len(findings)} open finding(s): "
            + "; ".join(f.summary for f in findings[:5]))
    if actions:
        parts.append(
            f"{len(actions)} action(s) in the last 48h: "
            + "; ".join(
                f"{a.verb} ({'dry-run' if a.dry_run else 'ok' if a.ok else 'failed'})"
                for a in actions[:5]))
    if files:
        parts.append(
            "Drive record: "
            + "; ".join(
                f"{f.get('name', '?')} ({f.get('job_id') or 'no job'})"
                for f in files[:3]))

    if not parts:
        return f"Nothing on {surname} in the last 48 hours."
    return f"{surname}: " + " ".join(parts)


def _matching_files(snapshot: dict[str, Any], needle: str
                     ) -> list[dict[str, Any]]:
    docwatch = snapshot.get("docwatch") or {}
    files = docwatch.get("files") or []
    return [f for f in files
            if needle in str(f.get("author_last", "")).lower()
            or needle in str(f.get("name", "")).lower()]


def _mentions(needle: str, *values: Any) -> bool:
    for value in values:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if needle in text.lower():
            return True
    return False


# -- formatting ---------------------------------------------------------

def _fmt_eastern(dt: datetime) -> str:
    local = dt.astimezone(EASTERN)
    return local.strftime("%-I:%M %p ET")


def _fmt_iso_eastern(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return _fmt_eastern(dt)


def _fmt_age(age_s: Any) -> str:
    try:
        seconds = int(age_s)
    except (TypeError, ValueError):
        return "unknown age"
    if seconds < 60:
        return f"{seconds}s old"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m old"
    hours = minutes // 60
    remainder = minutes % 60
    return f"{hours}h{remainder:02d}m old"


__all__ = ["Reply", "handle"]
