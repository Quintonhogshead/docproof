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
from pathlib import Path

from app.jobs import JobStore
from app.settings import Paths, get_api_key

from . import auth as authlib
from . import schedule as schedulelib
from .settings import GOOGLE_KEY, WatchSettings
from .state import STATE_FILE, WatchState, last_tick

log = logging.getLogger("docproof.app.watch.status")

# What each answer from `stages.classify` is called in front of a person. One
# copy, because the terminal and the panel saying different words about the
# same file is how a support conversation goes wrong.
PLAIN_STAGE = {"new": "to prepare", "done": "already prepared",
               "failed": "needs attention", "output": "DocProof wrote this",
               "skip": "not a manuscript"}

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
        "upload_notes": ws.upload_notes,
        "upload_failure_note": ws.upload_failure_note,
        "max_files_per_tick": ws.max_files_per_tick,
        "auto_ticks": ws.auto_ticks,
        "tick_every_minutes": ws.tick_every_minutes,
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
