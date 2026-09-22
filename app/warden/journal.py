"""The Warden's memory: one SQLite file, WAL mode, everything it has ever
seen or done.

Five tables. `findings` is the deduplicated state of "what's currently
wrong" — re-seeing the same (rule, key) bumps `last_seen` rather than
spawning a duplicate row, which is what stops a stuck-book alert repeating
itself every twenty minutes, and `resolve_missing` closes a finding the
instant a snapshot no longer reproduces it. `actions` is the undo log: every
verb call, its tier, whether it was a dry run, and the state it saw *before*
acting, so a person can always ask "what did you actually change". `messages`
is every iMessage and email in or out, plus what the harness itself said,
used to enforce the rate limit and to answer `why <surname>`. `requests` is
the approvals queue `approvals.py` sits on top of — numbered, one-shot,
expiring. `kv` is everything else that needs to survive a restart but isn't
worth its own table: `paused`, `quiet_until`, tick timestamps, sync cursors.

Style match with `galley/memory/store.py`: a single-writer connection opened
with `PRAGMA journal_mode=WAL`, `sqlite3.Row` for column access by name, and
an injectable clock so tests never depend on wall-clock time. Unlike that
store this one has no multi-version migration ladder yet — `CREATE TABLE IF
NOT EXISTS` is idempotent and there is exactly one schema so far; if the
Warden's schema ever needs to change under data already on the Mini, lift
`MIGRATIONS`/`user_version` from `galley/memory/store.py` wholesale rather
than inventing a second way to do it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("docproof.app.warden.journal")

DEFAULT_DB_NAME = "journal.sqlite"


class RequestPending(Exception):
    """Raised by `open_request` for a second `kind="code"` request while one
    is still open. One code change in flight at a time — see
    docs/monitoring-agent-plan.md, "When the fix is code"."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule         TEXT NOT NULL,
    key          TEXT NOT NULL,
    severity     TEXT NOT NULL,
    summary      TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    resolved_at  TEXT,
    notified_at  TEXT
);
-- Only one OPEN row per (rule, key): a finding that has been resolved and
-- comes back later gets a fresh row (fresh first_seen), not the old one
-- reopened, so the history of "this happened, was fixed, happened again"
-- stays visible.
CREATE UNIQUE INDEX IF NOT EXISTS ux_findings_open
    ON findings(rule, key) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_findings_resolved ON findings(resolved_at);

CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    verb         TEXT NOT NULL,
    args_json    TEXT NOT NULL DEFAULT '{}',
    tier         INTEGER NOT NULL DEFAULT 0,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    before_json  TEXT NOT NULL DEFAULT '{}',
    result_json  TEXT NOT NULL DEFAULT '{}',
    ok           INTEGER NOT NULL DEFAULT 1,
    finding_id   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_actions_verb ON actions(verb, at);
CREATE INDEX IF NOT EXISTS ix_actions_at ON actions(at);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    channel   TEXT NOT NULL,
    direction TEXT NOT NULL,
    party     TEXT NOT NULL DEFAULT '',
    text      TEXT NOT NULL DEFAULT '',
    ref       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_messages_channel_dir_at
    ON messages(channel, direction, at);

CREATE TABLE IF NOT EXISTS requests (
    number       INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'open',
    answered_at  TEXT,
    answered_by  TEXT,
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests(status);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class Finding:
    id: int
    rule: str
    key: str
    severity: str
    summary: str
    evidence: dict[str, Any]
    first_seen: str
    last_seen: str
    resolved_at: str | None
    notified_at: str | None


@dataclass(frozen=True)
class Action:
    id: int
    at: str
    verb: str
    args: dict[str, Any]
    tier: int
    dry_run: bool
    before: dict[str, Any]
    result: dict[str, Any]
    ok: bool
    finding_id: int | None


@dataclass(frozen=True)
class Request:
    number: int
    at: str
    kind: str
    text: str
    payload: dict[str, Any]
    status: str
    answered_at: str | None
    answered_by: str | None
    expires_at: str | None


def _as_clock(now: str | Callable[[], str] | None) -> Callable[[], str]:
    if now is None:
        return lambda: datetime.now(timezone.utc).isoformat()
    if callable(now):
        return now
    fixed = str(now)
    return lambda: fixed


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_dt(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else _parse(value)


def _finding_from_row(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"], rule=row["rule"], key=row["key"],
        severity=row["severity"], summary=row["summary"],
        evidence=json.loads(row["evidence_json"] or "{}"),
        first_seen=row["first_seen"], last_seen=row["last_seen"],
        resolved_at=row["resolved_at"], notified_at=row["notified_at"])


def _action_from_row(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"], at=row["at"], verb=row["verb"],
        args=json.loads(row["args_json"] or "{}"), tier=row["tier"],
        dry_run=bool(row["dry_run"]),
        before=json.loads(row["before_json"] or "{}"),
        result=json.loads(row["result_json"] or "{}"),
        ok=bool(row["ok"]), finding_id=row["finding_id"])


def _request_from_row(row: sqlite3.Row) -> Request:
    return Request(
        number=row["number"], at=row["at"], kind=row["kind"],
        text=row["text"], payload=json.loads(row["payload_json"] or "{}"),
        status=row["status"], answered_at=row["answered_at"],
        answered_by=row["answered_by"], expires_at=row["expires_at"])


class Journal:
    """A single-writer connection onto `journal.sqlite`. Use as a context
    manager, or call `close()` directly."""

    def __init__(self, path: str | Path, *,
                 now: str | Callable[[], str] | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)
        self._now = _as_clock(now)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- findings ---------------------------------------------------------

    def upsert_finding(self, rule: str, key: str, severity: str,
                        summary: str, evidence: dict[str, Any] | None
                        ) -> Finding:
        """Record that `rule`/`key` is currently firing.

        A second call for the same open (rule, key) bumps `severity`,
        `summary`, `evidence` and `last_seen` in place and leaves
        `first_seen`/`notified_at` alone — re-seeing a finding is not a new
        finding, and it must not reset whether it has already been notified
        about (see `mark_notified`, and the tick's "one text per new
        finding" rule)."""
        now = self._now()
        evidence_json = json.dumps(evidence or {})
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM findings WHERE rule=? AND key=? "
                "AND resolved_at IS NULL", (rule, key)).fetchone()
            if row is not None:
                self._conn.execute(
                    "UPDATE findings SET severity=?, summary=?, "
                    "evidence_json=?, last_seen=? WHERE id=?",
                    (severity, summary, evidence_json, now, row["id"]))
                return Finding(row["id"], rule, key, severity, summary,
                                evidence or {}, row["first_seen"], now,
                                None, row["notified_at"])
            cur = self._conn.execute(
                "INSERT INTO findings (rule, key, severity, summary, "
                "evidence_json, first_seen, last_seen, resolved_at, "
                "notified_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (rule, key, severity, summary, evidence_json, now, now))
            return Finding(cur.lastrowid, rule, key, severity, summary,
                            evidence or {}, now, now, None, None)

    def resolve_missing(self, rule_keys_seen: set[tuple[str, str]]
                         ) -> list[Finding]:
        """Close every open finding whose (rule, key) is not in
        `rule_keys_seen` — the set `rules.evaluate` actually fired this
        tick. Returns the findings just resolved, so the tick can send the
        one "resolved" text the plan calls for."""
        now = self._now()
        open_rows = self._conn.execute(
            "SELECT * FROM findings WHERE resolved_at IS NULL").fetchall()
        resolved: list[Finding] = []
        with self._conn:
            for row in open_rows:
                if (row["rule"], row["key"]) in rule_keys_seen:
                    continue
                self._conn.execute(
                    "UPDATE findings SET resolved_at=? WHERE id=?",
                    (now, row["id"]))
                f = _finding_from_row(row)
                resolved.append(
                    Finding(f.id, f.rule, f.key, f.severity, f.summary,
                            f.evidence, f.first_seen, f.last_seen, now,
                            f.notified_at))
        return resolved

    def open_findings(self) -> list[Finding]:
        rows = self._conn.execute(
            "SELECT * FROM findings WHERE resolved_at IS NULL "
            "ORDER BY id").fetchall()
        return [_finding_from_row(r) for r in rows]

    def mark_notified(self, finding_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE findings SET notified_at=? WHERE id=?",
                (self._now(), finding_id))

    # -- actions ------------------------------------------------------------

    def record_action(self, verb: str, args: dict[str, Any], tier: int,
                       dry_run: bool, before: dict[str, Any],
                       result: dict[str, Any], ok: bool,
                       finding_id: int | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO actions (at, verb, args_json, tier, dry_run, "
                "before_json, result_json, ok, finding_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._now(), verb, json.dumps(args or {}), tier,
                 int(bool(dry_run)), json.dumps(before or {}),
                 json.dumps(result or {}), int(bool(ok)), finding_id))
        return cur.lastrowid

    def recent_actions(self, hours: int = 24) -> list[Action]:
        cutoff = _iso(_as_dt(self._now()) - timedelta(hours=hours))
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE at >= ? ORDER BY at DESC",
            (cutoff,)).fetchall()
        return [_action_from_row(r) for r in rows]

    def last_action(self, verb: str, key: Any = None) -> Action | None:
        """The most recent action for `verb`, optionally narrowed to one
        whose `args` mentions `key` as one of its values (a book surname, a
        file id — whatever the verb's caller keyed it on). This is what a
        tick asks before re-running a Tier-0 fix, to enforce "not acted on
        in the last 6h" without a second index of "what this action was
        about"."""
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE verb=? ORDER BY id DESC",
            (verb,)).fetchall()
        for row in rows:
            if key is None:
                return _action_from_row(row)
            args = json.loads(row["args_json"] or "{}")
            if key in args.values() or key in (row["finding_id"],):
                return _action_from_row(row)
        return None

    # -- messages -----------------------------------------------------------

    def record_message(self, channel: str, direction: str, party: str,
                        text: str, ref: str = "") -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (at, channel, direction, party, "
                "text, ref) VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), channel, direction, party, text, ref))
        return cur.lastrowid

    def messages_since(self, channel: str, direction: str,
                        since: datetime | str) -> list[dict[str, Any]]:
        cutoff = _iso(_as_dt(since))
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE channel=? AND direction=? "
            "AND at >= ? ORDER BY at", (channel, direction, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]

    def texts_sent_last_hour(self) -> int:
        cutoff = _iso(_as_dt(self._now()) - timedelta(hours=1))
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE channel='imessage' "
            "AND direction='out' AND at >= ?", (cutoff,)).fetchone()
        return int(row["n"])

    # -- requests -------------------------------------------------------

    def open_request(self, kind: str, text: str, payload: dict[str, Any],
                      *, expires_at: datetime | str | None = None
                      ) -> Request:
        """Open a new numbered approval request. Numbers are 1-based and
        monotonic (SQLite's own AUTOINCREMENT), never reused.

        `expires_at` is optional and not part of the documented three-arg
        call — pass it when the caller (typically the tick, which knows
        `Thresholds.request_expiry_h`) wants this specific request to expire
        on its own via `expire_requests`; leave it unset and it only expires
        through `approvals.expire`'s `hours` fallback.

        Only one `kind="code"` request may be open at a time — see
        `RequestPending`."""
        if kind == "code":
            existing = self._conn.execute(
                "SELECT 1 FROM requests WHERE kind='code' AND status='open'"
            ).fetchone()
            if existing is not None:
                raise RequestPending(
                    "a code request is already open; answer or let it "
                    "expire first")
        now = self._now()
        expires = _iso(_as_dt(expires_at)) if expires_at is not None else None
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO requests (at, kind, text, payload_json, "
                "status, answered_at, answered_by, expires_at) "
                "VALUES (?, ?, ?, ?, 'open', NULL, NULL, ?)",
                (now, kind, text, json.dumps(payload or {}), expires))
        return Request(cur.lastrowid, now, kind, text, payload or {},
                        "open", None, None, expires)

    def get_request(self, number: int) -> Request | None:
        row = self._conn.execute(
            "SELECT * FROM requests WHERE number=?", (number,)).fetchone()
        return _request_from_row(row) if row is not None else None

    def answer_request(self, number: int, answer: str, who: str) -> Request:
        if answer not in ("yes", "no"):
            raise ValueError(f"answer must be 'yes' or 'no', got {answer!r}")
        row = self._conn.execute(
            "SELECT * FROM requests WHERE number=?", (number,)).fetchone()
        if row is None:
            raise KeyError(f"no request #{number}")
        if row["status"] != "open":
            raise ValueError(
                f"request #{number} is {row['status']}, not open")
        now = self._now()
        with self._conn:
            self._conn.execute(
                "UPDATE requests SET status=?, answered_at=?, "
                "answered_by=? WHERE number=?",
                (answer, now, who, number))
        return Request(number, row["at"], row["kind"], row["text"],
                        json.loads(row["payload_json"] or "{}"), answer,
                        now, who, row["expires_at"])

    def open_requests(self) -> list[Request]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status='open' ORDER BY number"
        ).fetchall()
        return [_request_from_row(r) for r in rows]

    def answered_requests(self, answer: str) -> list[Request]:
        """Every request answered `answer` ("yes" or "no"), oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status=? ORDER BY number", (answer,)
        ).fetchall()
        return [_request_from_row(r) for r in rows]

    def expire_requests(self, now: datetime | str, *,
                         default_hours: int | None = None
                         ) -> list[Request]:
        """Mark stale open requests `expired`. A request with an explicit
        `expires_at` (set at `open_request` time) expires the moment `now`
        passes it. A request without one only expires here when the caller
        supplies `default_hours` — computed against its own `at` — which is
        how `approvals.expire(journal, now, hours)` covers requests opened
        without a precomputed deadline. Returns the requests just expired."""
        now_dt = _as_dt(now)
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status='open'").fetchall()
        expired: list[Request] = []
        with self._conn:
            for row in rows:
                deadline: datetime | None
                if row["expires_at"]:
                    deadline = _parse(row["expires_at"])
                elif default_hours is not None:
                    deadline = _parse(row["at"]) + timedelta(hours=default_hours)
                else:
                    deadline = None
                if deadline is None or now_dt < deadline:
                    continue
                self._conn.execute(
                    "UPDATE requests SET status='expired' WHERE number=?",
                    (row["number"],))
                expired.append(Request(
                    row["number"], row["at"], row["kind"], row["text"],
                    json.loads(row["payload_json"] or "{}"), "expired",
                    row["answered_at"], row["answered_by"],
                    row["expires_at"]))
        return expired

    # -- kv -------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set(self, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    # -- summary ----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """The `journal` section of a snapshot (see snapshot.py)."""
        return {
            "open_findings": [_finding_dict(f) for f in self.open_findings()],
            "open_requests": [_request_dict(r) for r in self.open_requests()],
            "recent_actions": [_action_dict(a) for a in self.recent_actions()],
            "paused": self.get("paused") in ("1", "true", "True"),
            "quiet_until": self.get("quiet_until"),
            "last_tick_at": self.get("last_tick_at"),
        }


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "id": f.id, "rule": f.rule, "key": f.key, "severity": f.severity,
        "summary": f.summary, "evidence": f.evidence,
        "first_seen": f.first_seen, "last_seen": f.last_seen,
        "resolved_at": f.resolved_at, "notified_at": f.notified_at,
    }


def _action_dict(a: Action) -> dict[str, Any]:
    return {
        "id": a.id, "at": a.at, "verb": a.verb, "args": a.args,
        "tier": a.tier, "dry_run": a.dry_run, "before": a.before,
        "result": a.result, "ok": a.ok, "finding_id": a.finding_id,
    }


def _request_dict(r: Request) -> dict[str, Any]:
    return {
        "number": r.number, "at": r.at, "kind": r.kind, "text": r.text,
        "payload": r.payload, "status": r.status,
        "answered_at": r.answered_at, "answered_by": r.answered_by,
        "expires_at": r.expires_at,
    }


__all__ = [
    "Journal", "Finding", "Action", "Request", "RequestPending",
    "DEFAULT_DB_NAME",
]
