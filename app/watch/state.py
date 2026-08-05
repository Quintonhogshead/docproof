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
