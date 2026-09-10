"""Inspect and reset the per-workflow markers that prevent repeat processing."""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone

from . import drive
from .state import FileRecord, WatchState
from .stages import (STATE_PROP, PROOF_PROP, PROMO_PROP, PLAN_PROP,
                     CORRECTIONS_PROP)

# stage -> label, local marker, Drive marker, terminal values
STAGES = {
    "format": ("Formatting", "marked", STATE_PROP, ("formatted", "failed")),
    "proof": ("Proofreading", "proof_marked", PROOF_PROP,
              ("done", "human", "failed")),
    "promo": ("Promo", "promo_marked", PROMO_PROP, ("done", "delivered", "failed")),
    "plan": ("Marketing plan", "plan_marked", PLAN_PROP, ("done", "delivered", "failed")),
    "corrections": ("Interior corrections", "corrections_marked", CORRECTIONS_PROP,
                    ("done", "failed")),
}


def for_record(rec: FileRecord) -> list[dict]:
    return [{"stage": stage, "label": label, "value": getattr(rec, field),
             "completed": getattr(rec, field) != "failed"}
            for stage, (label, field, _, terminal) in STAGES.items()
            if getattr(rec, field) in terminal]


def reset(token: str, rec: FileRecord, state: WatchState, stage: str, *,
          who: str, opener=None) -> None:
    """Clear one terminal workflow, keeping other workflows and past jobs."""
    label, field, prop, terminal = STAGES[stage]
    if getattr(rec, field) not in terminal:
        raise ValueError(f"This book has no finished {label.lower()} flag to clear.")
    at = datetime.now(timezone.utc).isoformat()
    # Write the reset time durably too: old outputs must not be adopted as the
    # new run's work if local state is restored from the Drive folder later.
    drive.set_app_properties(token, rec.file_id,
                             {prop: None, f"docproof.reset.{stage}": at},
                             opener=opener or drive._open_url)
    _reset_record(rec, stage, at=at, who=who)
    state.record(rec)


def _reset_record(rec: FileRecord, stage: str, *, at: str, who: str) -> None:
    defaults = FileRecord(file_id=rec.file_id)
    owned = ({"job_id", "marked", "attempts", "uploaded", "hubspot_id",
              "hubspot_done", "completion_emailed"} if stage == "format"
             else {f.name for f in fields(FileRecord)
                   if f.name.startswith(stage + "_")})
    rec.flag_reset_history.append({
        "stage": stage, "at": at, "by": who,
        "previous": {key: getattr(rec, key) for key in sorted(owned)},
    })
    for key in owned:
        setattr(rec, key, getattr(defaults, key))
    rec.flag_resets[stage] = at


def reset_time(rec: FileRecord, stage: str, file: drive.DriveFile) -> str:
    return rec.flag_resets.get(stage) or file.app_properties.get(
        f"docproof.reset.{stage}", "")


def remember(file: drive.DriveFile, state: WatchState, **routing: str) -> None:
    """Include known Drive markers in History even after a local-state restore."""
    rec = state.get(file.id)
    changed = False
    for stage, (_, field, prop, terminal) in STAGES.items():
        at = file.app_properties.get(f"docproof.reset.{stage}")
        if at and rec.flag_resets.get(stage) != at:
            # Recover a reset that reached Drive before the local save. Clear
            # the old job pointers as well as restoring the timestamp.
            _reset_record(rec, stage, at=at, who="recovered from Drive")
            changed = True
        value = file.app_properties.get(prop)
        if value in terminal and getattr(rec, field) != value:
            setattr(rec, field, value)
            changed = True
    if changed:
        rec.name = file.name
        for key, value in routing.items():
            setattr(rec, key, value)
        state.record(rec)


def current_outputs(listing: list[drive.DriveFile], rec: FileRecord,
                    stage: str, file: drive.DriveFile) -> list[drive.DriveFile]:
    """After an explicit reset, only reuse outputs written after that reset."""
    at = reset_time(rec, stage, file)
    if not at:
        return listing
    cutoff = datetime.fromisoformat(at.replace("Z", "+00:00"))
    fresh = []
    for candidate in listing:
        try:
            modified = datetime.fromisoformat(candidate.modified_time.replace("Z", "+00:00"))
            if modified > cutoff:
                fresh.append(candidate)
        except (ValueError, TypeError):
            continue  # an undated output cannot prove it belongs to the new run
    return fresh
