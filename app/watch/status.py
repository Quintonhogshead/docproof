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
        # The proofing stage, whole: the Automations panel's Proofread drawer
        # both draws from and writes back every one of these.
        "proofing_enabled": ws.proofing_enabled,
        "proof_runner": ws.proof_runner,
        "hubspot_proof_ready_value": ws.hubspot_proof_ready_value,
        "hubspot_proof_done_value": ws.hubspot_proof_done_value,
        "hubspot_proof_needs_human_value": ws.hubspot_proof_needs_human_value,
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
    }


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
            # Proofing's own lifecycle, beside formatting's rather than mixed
            # into it: a book can be formatted and still out with a
            # proofreader, and "" here simply means proofing never touched it.
            # `proof_marked` is the live state ("awaiting" while a practitioner
            # has the book); `proof_outcome` is the verdict once one landed.
            "proof_marked": rec.proof_marked,
            "proof_outcome": rec.proof_outcome,
            "proof_reason": rec.proof_outcome_reason,
            "proof_uploaded": list(rec.proof_uploaded),
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
