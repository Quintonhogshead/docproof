"""What the watcher already did, kept locally so a crash costs nothing.

The markers on the Drive files are the real record — they survive a renamed
folder, a new Mac, a move to a server. This file is the smaller question the
markers cannot answer: a tick that died between preparing a manuscript and
uploading the result left a paid-for job on disk and no sign of it in Drive.
Without this, the next tick would prepare it again and pay again.

So every step writes here before the step after it runs, and every write is
atomic — the whole point is surviving the process dying at an arbitrary moment.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("docproof.app.watch.state")

STATE_FILE = "state.json"
LAST_TICK = "last_tick"
VERSION = 1


@dataclass
class FileRecord:
    """One manuscript in the watched folder, as far as the watcher got."""

    file_id: str
    name: str = ""
    job_id: str = ""
    # Bumped only by failures worth retrying. A verification failure is not one
    # of those: it is marked in Drive and never counted here.
    attempts: int = 0
    # Artifact name → the Drive id it was uploaded as. Skipping what is already
    # here is what makes a resumed upload cost nothing.
    uploaded: dict[str, str] = field(default_factory=dict)
    marked: str = ""            # "" | "formatted" | "failed"
    # The CRM record this manuscript's key resolved to, written before prep so a
    # crash between the HubSpot lookup and the Drive marker still knows which
    # record to finish. Empty for a manuscript prepared before HubSpot was
    # turned on, which is how the completion sweep leaves those alone.
    hubspot_id: str = ""
    # Whether the completion toggle has been set. Recorded before the Drive
    # marker, so a file that is `formatted` in Drive was `done` in HubSpot first.
    hubspot_done: bool = False
    # Whether the full-log completion email has gone out for this book. Set only
    # once the send is confirmed, so a book whose Drive marker is lost — or
    # invisible to a different OAuth client — is not emailed about a second time
    # when it is reconsidered, while a send that merely failed still retries.
    completion_emailed: bool = False
    # Subfolder routing (only written when `subfolders_enabled`). The author's
    # name as HubSpot gave it and the subfolder it resolved to, stamped before
    # prep so a job that spans ticks — or crashes mid-flight — is finished into
    # the same folder it started in, without asking HubSpot or Drive to resolve
    # it again. Empty on a flat-folder install, which is how those stay routed
    # to `folder_id`.
    author_first: str = ""
    author_last: str = ""
    subfolder_id: str = ""
    subfolder_name: str = ""
    # Promo lives on the same file id but its own lifecycle, so it keeps its own
    # fields rather than sharing formatting's. All empty on a record written
    # before promo existed, which is how those stay formatting-only. The Drive
    # `docproof.promo` marker is the durable record; these are the local shortcut
    # that stops a crash mid-flight from re-paying for the copy.
    promo_job_id: str = ""
    promo_hubspot_id: str = ""          # the ready record promo matched
    promo_hubspot_done: bool = False    # the "finished" value has been written
    promo_marked: str = ""             # "" | "pending" | "delivered" | "failed"
    promo_attempts: int = 0
    promo_uploaded: dict[str, str] = field(default_factory=dict)
    # The marketing plan lives on the same file id but its own lifecycle again —
    # the promo twin — so it keeps its own fields rather than sharing promo's or
    # formatting's. All empty on a record written before the plan stage existed.
    # The Drive `docproof.plan` marker is the durable record; these are the local
    # shortcut that stops a crash mid-flight from re-paying for the plan.
    plan_job_id: str = ""
    plan_hubspot_id: str = ""           # the record flagged "Needed" it matched
    # The author's display / pen name, captured from that record when it matched
    # so the plan can be headed with it without a second HubSpot round-trip — and
    # persisted before the job is created, so a crash between the two does not
    # lose it. Empty heads the plan with the title alone.
    plan_pen: str = ""
    plan_hubspot_done: bool = False     # the "Uploaded" value has been written
    plan_marked: str = ""              # "" | "pending" | "delivered" | "failed"
    plan_attempts: int = 0
    plan_uploaded: dict[str, str] = field(default_factory=dict)
    # Proofing, on its own lifecycle again — the same manuscript, a second pass
    # over it, its own HubSpot value and its own Drive marker. All empty on a
    # record written before the proofing stage existed, which is how those stay
    # formatting-only. The Drive `docproof.proof` marker is the durable record;
    # these are the local shortcut that stops a crash mid-flight from re-paying
    # for the read.
    proof_job_id: str = ""
    proof_hubspot_id: str = ""          # the record flagged ready it matched
    # Whether "Proofing Complete" has been written. The guard that makes the
    # flip happen exactly once, recorded before the Drive marker so a book that
    # reads `done` in Drive was done in HubSpot first. Stays False forever on a
    # needs_human verdict, which writes nothing.
    proof_hubspot_done: bool = False
    proof_marked: str = ""             # "" | "awaiting" | "done" | "human" | "failed"
    proof_attempts: int = 0
    proof_uploaded: dict[str, str] = field(default_factory=dict)
    # The verdict and the sentence behind it, kept so `status` and a later tick
    # can say why a book was left for a person without re-reading Drive.
    proof_outcome: str = ""            # "" | "done" | "needs_human"
    proof_outcome_reason: str = ""
    # Whether the owner has been told this book is waiting on an external
    # practitioner. One email per book, not one per tick.
    proof_awaiting_emailed: bool = False
    modified_time: str = ""
    updated_at: str = ""


class WatchState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.files: dict[str, FileRecord] = {}

    @classmethod
    def load(cls, path: str | Path) -> "WatchState":
        """Read what earlier ticks recorded. An unreadable file starts clean:
        the markers in Drive still prevent the expensive mistake, and refusing
        to run because a cache file is corrupt would be the worse failure."""
        state = cls(path)
        if not state.path.is_file():
            return state
        try:
            raw = json.loads(state.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable watch state (%s); starting "
                        "clean.", e)
            return state
        known = {f.name for f in fields(FileRecord)}
        for file_id, entry in (raw.get("files") or {}).items():
            if not isinstance(entry, dict):
                log.warning("Skipping malformed state entry %s", file_id)
                continue
            data = {k: v for k, v in entry.items() if k in known}
            data["file_id"] = file_id
            try:
                state.files[file_id] = FileRecord(**data)
            except TypeError as e:
                log.warning("Skipping malformed state entry %s (%s)",
                            file_id, e)
        return state

    def get(self, file_id: str) -> FileRecord:
        """This file's record, empty if it has none. Not saved until asked."""
        return self.files.get(file_id) or FileRecord(file_id=file_id)

    def record(self, rec: FileRecord) -> None:
        rec.updated_at = datetime.now(timezone.utc).isoformat()
        self.files[rec.file_id] = rec
        self.save()

    def forget(self, file_id: str) -> None:
        if self.files.pop(file_id, None) is not None:
            self.save()

    def save(self) -> None:
        body = json.dumps({
            "version": VERSION,
            "files": {k: asdict(v) for k, v in self.files.items()},
        }, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(self.path.name + ".writing")
        staging.write_text(body, encoding="utf-8")
        os.replace(staging, self.path)


#
# One timestamp, in its own small file, because two clocks now ask for it: the
# launchd agent runs whether or not the app is open, and a timer inside the app
# picks up the time a sleeping Mac slept through. Neither should repeat what the
# other just did.

def note_tick(home: str | Path) -> None:
    """Say that a pass has started.

    Written at the start rather than the end, deliberately. A pass can run for
    hours; a stamp written when it finished would read as "never looked" for
    every one of them, which is exactly the window in which the other clock
    would decide to start a second pass."""
    path = Path(home) / LAST_TICK
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError as e:                  # noqa: BLE001 - a stamp is not the job
        log.warning("Could not record when the watcher last looked (%s)", e)


def last_tick(home: str | Path) -> datetime | None:
    """When a pass last started, or None if one never has."""
    path = Path(home) / LAST_TICK
    try:
        return datetime.fromisoformat(path.read_text("utf-8").strip())
    except (OSError, ValueError):
        return None
