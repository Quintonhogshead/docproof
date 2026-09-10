"""What the watcher has been doing, in a shape either front end can render.

The terminal prints this and the panel draws it, so the join that produces it
lives here rather than in whichever one was written first. There are two
records to put together: what the watcher did with each file, kept in
`state.json` and keyed by Drive id, and what each run cost, kept in the job
store and keyed by job id.

Nothing here creates anything. The panel asks this question every five seconds,
and a question should not make folders.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.jobs import JobStore
from app.settings import Paths, get_api_key

from . import auth as authlib
from . import daily
from . import schedule as schedulelib
from .schedule import ScheduleError
from .settings import GOOGLE_KEY, WatchSettings
from .stages import PREVIEW_GATED
from .state import STATE_FILE, WatchState, last_tick

log = logging.getLogger("docproof.app.watch.status")

# What each answer from `stages.classify` is called in front of a person. One
# copy, because the terminal and the panel saying different words about the
# same file is how a support conversation goes wrong.
PLAIN_STAGE = {"new": "to prepare", "proof": "to proofread",
               # The two automations `classify` knows nothing about. A dry run
               # itemizes them so "what a pass would do" speaks for every
               # workflow, not only formatting; a real pass never emits them.
               "promo": "to write promo copy for",
               "plan": "to write a marketing plan for",
               "done": "already prepared",
               "failed": "needs attention", "output": "DocProof wrote this",
               "skip": "not a manuscript"}

# What a row says when the dry run could not apply that stage's HubSpot gate:
# the file is a candidate, and whether the pass acts on it is the CRM's answer,
# not the folder's. Said out loud rather than implied — a preview that promised
# to prepare eleven books and then prepared one would be worse than no preview.
GATED_NOTE = " — if HubSpot says so"


def plain_stage(stage: str) -> str:
    """The words for one dry-run row, gate and all.

    `report.plan` carries a stage value, optionally marked `PREVIEW_GATED`; this
    is the single place that turns either into English, so the terminal and the
    panel cannot describe the same row differently."""
    gated = stage.endswith(PREVIEW_GATED)
    base = base_stage(stage)
    return PLAIN_STAGE.get(base, base) + (GATED_NOTE if gated else "")


def base_stage(stage: str) -> str:
    """A dry-run row's stage without its gate mark, for counting by kind."""
    return stage[:-len(PREVIEW_GATED)] if stage.endswith(PREVIEW_GATED) else stage


# What a file's marker means, said plainly. `marked` is the watcher's own word
# for what it did; anything else is a job state and speaks for itself.
PLAIN_MARK = {"formatted": "Prepared", "failed": "Needs attention"}


def missing(ws: WatchSettings, *, get_key=None) -> str | None:
    """What has to exist before a pass can do anything: "folder", "auth", or
    nothing.

    Deliberately a word rather than a sentence. The terminal answers it by
    naming a command and the panel by naming a card, and neither should be
    reading the other's prose to find out what is wrong."""
    if not ws.folder_id:
        return "folder"
    read = get_key or get_api_key
    if not ws.client_id or not ws.client_secret or not read(GOOGLE_KEY):
        return "auth"
    return None


def status(home: str | Path, *, get_key=None,
           agent_path: Path | None = None) -> dict:
    """Everything there is to say about this watcher, without touching Drive."""
    root = Path(home)
    ws = WatchSettings.load(root)
    read = get_key or get_api_key
    signed = authlib.token_source(read, bool(ws.client_id))
    times = schedulelib.current(path=agent_path) if _agent_readable() else None
    stamp = last_tick(root)

    return {
        "home": str(root),
        "folder_id": ws.folder_id,
        "model": ws.model,
        "prep_output": ws.prep_output,
        "upload_failure_note": ws.upload_failure_note,
        # Subfolder mode is set up from the CLI, but the web panel surfaces the
        # one knob inside it that a person changes book to book — whether to
        # prepare only "<surname> - Book Original" — so it also needs to know the
        # mode is on to decide whether to show it.
        "subfolders_enabled": ws.subfolders_enabled,
        "require_source_label": ws.require_source_label,
        # Whether the CRM gate is on at all. The panel needs it to say why a
        # HubSpot-triggered workflow cannot run yet, and it is a fact about this
        # watcher rather than about promo — which is where the panel used to
        # have to go and borrow it from.
        "hubspot_enabled": ws.hubspot_enabled,
        "hubspot_write_back": ws.hubspot_write_back,
        "corrections_native_shared_form": ws.corrections_native_shared_form,
        # The proofing stage, whole: the Automations panel's Proofread drawer
        # both draws from and writes back every one of these.
        "proofing_enabled": ws.proofing_enabled,
        "proof_runner": ws.proof_runner,
        "hubspot_proof_ready_value": ws.hubspot_proof_ready_value,
        "hubspot_proof_done_value": ws.hubspot_proof_done_value,
        "hubspot_proof_needs_human_value": ws.hubspot_proof_needs_human_value,
        # The interior-corrections stage, whole, for the panel's drawer.
        "corrections_enabled": ws.corrections_enabled,
        "corrections_engine": ws.corrections_engine,
        **{key: value for key, value in vars(ws).items() if key.startswith("corrections_native_")},
        "native_correction_jobs": native_correction_jobs(home),
        "native_worker": native_worker(root),
        "native_intake": native_intake(root),
        "hubspot_corrections_ready_value": ws.hubspot_corrections_ready_value,
        "hubspot_corrections_done_value": ws.hubspot_corrections_done_value,
        "hubspot_corrections_file_property": ws.hubspot_corrections_file_property,
        "hubspot_corrections_text_property": ws.hubspot_corrections_text_property,
        "corrections_folder_name": ws.corrections_folder_name,
        "corrections_model_passes": ws.corrections_model_passes,
        "max_files_per_tick": ws.max_files_per_tick,
        "auto_ticks": ws.auto_ticks,
        "tick_every_minutes": ws.tick_every_minutes,
        "tick_at_times": list(ws.tick_at_times),
        "tick_timezone": ws.tick_timezone,
        "next_tick_at": _next_tick(ws),
        "archive_enabled": ws.archive_enabled,
        "archive_folder_id": ws.archive_folder_id,
        "archive_include_source": ws.archive_include_source,
        "signed_in": signed["configured"],
        "token_source": signed["source"],
        "has_client": bool(ws.client_id and ws.client_secret),
        "missing": missing(ws, get_key=read),
        "times": [f"{h:02d}:{m:02d}" for h, m in times] if times else [],
        "last_tick_at": stamp.isoformat() if stamp else None,
        "files": _files(root),
        # The practitioner agent's last heartbeat (galley/agent.py), so the
        # Proofread drawer can say what the machine is doing right now.
        "agent": agent_status(root),
    }


AGENT_STATUS_FILE = "agent-status.json"

NATIVE_WORKER_FILE = "native-worker.json"
NATIVE_INTAKE_FILE = "native-intake.json"


def _receipt_text(value, *, limit: int = 500) -> str | None:
    """Keep status receipts useful without exposing arbitrary payloads."""
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _receipt_count(report: dict, name: str) -> int:
    value = report.get(name, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, list):
        return len(value)
    return 0


def native_worker(home: str | Path) -> dict:
    """Return the last Mac native-worker heartbeat without its raw report.

    This is deliberately separate from ``last_tick_at``: the native worker is
    an opt-in process on the Mac and may be checked while the general Drive
    watcher is stopped.  Only state, timestamps, bounded error text and report
    counts leave the local receipt.
    """
    path = Path(home) / NATIVE_WORKER_FILE
    empty = {"state": "paused", "started_at": None, "finished_at": None,
             "error": None, "report": {}}
    try:
        payload = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        return {**empty, "state": "error",
                "error": "The native worker status receipt could not be read."}
    if not isinstance(payload, dict):
        return {**empty, "state": "error",
                "error": "The native worker status receipt is invalid."}
    state = payload.get("state")
    if state not in {"paused", "checking", "idle", "error", "attention"}:
        state = "error"
    raw_report = payload.get("report")
    report = raw_report if isinstance(raw_report, dict) else {}
    counts = {name: _receipt_count(report, name) for name in (
        "listed", "waiting", "corrected", "uploaded", "failed",
        "needs_human")}
    return {"state": state,
            "local_only": payload.get("local_only") is True,
            "drive_uploads_enabled": payload.get("drive_uploads_enabled") is True,
            "started_at": _receipt_text(payload.get("started_at"), limit=100),
            "finished_at": _receipt_text(payload.get("finished_at"), limit=100),
            "error": _receipt_text(payload.get("error")),
            "report": counts}


def _native_intake_row(row: dict) -> dict:
    """Copy only display fields from an unmatched submission receipt."""
    allowed = ("id", "submission_id", "conversion_id", "first_name",
               "last_name", "book", "identity", "reason", "submitted_at",
               "received_at", "file_name")
    result = {}
    for key in allowed:
        value = _receipt_text(row.get(key))
        if value:
            result[key] = value
    if not result:
        result["reason"] = "Unmatched correction submission needs review."
    return result


def native_intake(home: str | Path) -> dict:
    """Return unmatched native-form submissions for the corrections panel."""
    empty = {"unmatched": [], "count": 0, "checked_at": None, "error": None}
    path = Path(home) / NATIVE_INTAKE_FILE
    try:
        payload = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        return {**empty, "error": "The native intake receipt could not be read."}
    if not isinstance(payload, dict):
        return {**empty, "error": "The native intake receipt is invalid."}
    rows = payload.get("unmatched")
    if not isinstance(rows, list):
        rows = payload.get("items") if isinstance(payload.get("items"), list) else []
    unmatched = [_native_intake_row(row) for row in rows if isinstance(row, dict)]
    return {"unmatched": unmatched, "count": len(unmatched),
            "checked_at": _receipt_text(payload.get("checked_at")
                                         or payload.get("last_checked_at")
                                         or payload.get("finished_at"), limit=100),
            "error": _receipt_text(payload.get("error"))}
#: A heartbeat older than this is shown as stale: the machine is down, the
#: token changed, or it cannot reach us. Three polls plus slack.
AGENT_STALE_AFTER_S = 20 * 60


def save_agent_status(home: str | Path, payload: dict) -> Path:
    """Keep the newest heartbeat, atomically, with the time we received it."""
    import json
    import os
    import tempfile

    root = Path(home)
    root.mkdir(parents=True, exist_ok=True)
    target = root / AGENT_STATUS_FILE
    record = dict(payload)
    record["received_at"] = datetime.now(timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(prefix=".agent-status-", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def agent_status(home: str | Path, *, now: datetime | None = None
                 ) -> dict | None:
    """The last heartbeat, with how old it is and whether that is too old."""
    import json

    path = Path(home) / AGENT_STATUS_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    moment = now or datetime.now(timezone.utc)
    age = None
    try:
        seen = datetime.fromisoformat(str(record.get("received_at")))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = max(0.0, (moment - seen).total_seconds())
    except (TypeError, ValueError):
        pass
    interval = float(record.get("poll_interval_s") or 0) or 300.0
    stale_after = max(AGENT_STALE_AFTER_S, 3 * interval)
    record["age_s"] = age
    record["stale"] = age is None or age > stale_after
    return record


#: What `proof_marked` says while a book is out with a practitioner.
AWAITING = "awaiting"


def awaiting(home: str | Path) -> list[dict]:
    """The books out with an external practitioner, and where to find them.

    The Mac-side agent's whole view of the server: which manuscripts DocWatch
    has marked `awaiting`, the Drive id of each Book 1 to download, and the
    author folder the Book 2 set goes back to. Deliberately NOT the status
    payload — that carries the watcher's settings and every file it has ever
    seen, and the poller needs neither. Nothing here is writable, and nothing
    here names a HubSpot property or value.

    A record whose `proof_marked` has moved on (done, human, failed) is not
    listed: the book is finished, whatever the folder still holds. Reads
    `state.json` only — no Drive call, so a poll every five minutes costs
    the server nothing."""
    root = Path(home)
    ws = WatchSettings.load(root)
    state = WatchState.load(root / STATE_FILE)
    out = []
    for rec in sorted(state.files.values(), key=lambda r: r.updated_at):
        if rec.proof_marked != AWAITING:
            continue
        out.append({
            "file_id": rec.file_id,
            "name": rec.name,
            # Where the hand-off goes back. Subfolder mode routes each author
            # to their own folder; a flat install has one folder for everyone.
            "folder_id": rec.subfolder_id or ws.folder_id,
            "subfolder_id": rec.subfolder_id,
            "author_last": rec.author_last,
            "modified_time": rec.modified_time,
            "request_id": rec.flag_resets.get("proof", ""),
            "updated_at": rec.updated_at,
        })
    return out


def _next_tick(ws: WatchSettings) -> str | None:
    """When the in-app clock will next look, as ISO UTC, for the panel to show.

    Only for the fixed-times clock and only while it is on: the interval clock's
    next look depends on when the last one ran, which the panel already shows,
    and an off clock has no next look to promise. A bad hand-edited schedule
    says nothing rather than raising — the panel is not where that gets fixed."""
    if not (ws.auto_ticks and ws.tick_at_times):
        return None
    try:
        nxt = daily.next_run(ws.tick_at_times, ws.tick_timezone,
                             now=datetime.now(timezone.utc))
    except ScheduleError:
        return None
    return nxt.isoformat() if nxt else None


def _agent_readable() -> bool:
    """Reading a launch agent is a file read, and safe anywhere. Asking for one
    on a machine with no LaunchAgents folder is simply no."""
    try:
        return schedulelib.agents_dir().parent.is_dir()
    except OSError:                       # pragma: no cover - a home that isn't
        return False


def _files(root: Path) -> list[dict]:
    """One row per manuscript the watcher has seen, newest first."""
    from .flags import for_record

    state = WatchState.load(root / STATE_FILE)
    if not state.files:
        return []
    jobs = {job.id: job for job in _jobs(root)}
    rows = []
    for rec in sorted(state.files.values(), key=lambda r: r.updated_at,
                      reverse=True):
        job = jobs.get(rec.job_id)
        said = rec.marked or (job.state if job else "in progress")
        rows.append({
            "file_id": rec.file_id,
            "name": rec.name,
            "flags": for_record(rec),
            "flag_resets": rec.flag_resets,
            "author": " ".join(p for p in (rec.author_first, rec.author_last) if p),
            "job_id": rec.job_id,
            "marked": rec.marked,
            "said": said,
            "plain_state": PLAIN_MARK.get(rec.marked) or (
                job.plain_state() if job else "Being looked at"),
            "cost": job.cost if job else None,
            "words": job.words if job else None,
            "done": job.done if job else 0,
            "total": job.total if job else 0,
            "error": job.error if job else None,
            "uploaded": list(rec.uploaded),
            # Which author's folder this book is in, when the watcher is running
            # in subfolder mode. Blank on a flat install, and blank on a record
            # written before subfolders existed — the panel falls back to the
            # watched folder rather than showing an empty cell.
            "folder": rec.subfolder_name,
            # The ids behind that name. The proofing agent needs them to put a
            # hand-off back in the right author's folder, and a panel showing
            # "which folder" should be able to link to it.
            "subfolder_id": rec.subfolder_id,
            "author_last": rec.author_last,
            # Proofing's own lifecycle, beside formatting's rather than mixed
            # into it: a book can be formatted and still out with a
            # proofreader, and "" here simply means proofing never touched it.
            # `proof_marked` is the live state ("awaiting" while a practitioner
            # has the book); `proof_outcome` is the verdict once one landed.
            "proof_marked": rec.proof_marked,
            "proof_outcome": rec.proof_outcome,
            "proof_reason": rec.proof_outcome_reason,
            "proof_uploaded": list(rec.proof_uploaded),
            "corrections_marked": rec.corrections_marked,
            "corrections_input": rec.corrections_input_kind,
            "corrections_uploaded": list(rec.corrections_uploaded),
            "attempts": rec.attempts,
            "updated_at": rec.updated_at,
        })
    return rows


def _jobs(root: Path) -> list:
    """The watcher's own job records, or none.

    Asked for lazily: `JobStore` builds the folders it reads, and a watcher
    nobody has used yet should not acquire a `jobs/` directory because
    somebody opened a tab."""
    if not (root / "jobs").is_dir():
        return []
    try:
        return JobStore(Paths(root)).all()
    except OSError as e:                  # noqa: BLE001 - a status is not the job
        log.warning("Could not read what the watcher has done (%s)", e)
        return []


def native_correction_jobs(home) -> list[dict]:
    """Compact, nonsecret correction receipts for the Automations panel."""
    import json
    rows = []
    for path in Path(home).glob("native_jobs/*/job.json"):
        try:
            data = json.loads(path.read_text())
            result = data.get("result") or {}
            rows.append({"job_id": data.get("job_id", path.parent.name),
                         "book": data.get("source_name", "Book"),
                         "status": data.get("status", "queued"),
                         "needs_designer": result.get("needs_designer"),
                         "reasons": result.get("reasons", []) or ([data["reason"]] if data.get("reason") else []),
                         "counts": result.get("counts", {}),
                         "missing_attachments": [{"file_id": str(item.get("file_id", "")),
                                                   "filename": str(item.get("filename", "Downloaded correction file"))}
                                                  for item in data.get("missing_attachments", []) if isinstance(item, dict)],
                         "outputs": [name for name, key in {"indd": "output_indd", "pdf": "output_pdf", "package": "output_package", "report": "report", "spreadsheet": "audit_spreadsheet"}.items() if result.get(key)],
                         "uploaded": data.get("uploaded", {}),
                         "created_at": data.get("created_at", "")})
        except (ValueError, OSError):
            rows.append({"job_id": path.parent.name, "book": "Unreadable job receipt",
                         "status": "technical_block", "needs_designer": None,
                         "reasons": ["Inspect the saved job receipt before resuming."]})
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)[:100]
