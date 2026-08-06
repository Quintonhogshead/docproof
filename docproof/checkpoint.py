"""What a run has already paid for, kept where a restart can find it.

A synchronous review is one API call per (pass, chunk); prep is one per
window. Both used to be all-or-nothing: quit the app mid-run and the next
launch started again from zero, paying for every call a second time. The
manuscript that prompted this was reviewed start-to-partway four times in one
afternoon, billed each time.

So each completed call's results are written here as they land. A resumed run
replays them in the exact order the original run produced them — order
matters, because the validator gives earlier findings first claim on a span —
and only calls the API for what is missing.

The fingerprint is what keeps a checkpoint honest. Chunking and windowing are
deterministic in the document and the config, so cached results are only
reusable while both are unchanged: edit the manuscript, switch the model, or
touch a prompt, and every cached answer is stale. A mismatch wipes the file
and the run starts clean, which costs money but never correctness.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from pathlib import Path

from .models import Anchor, Finding, Usage

log = logging.getLogger("docproof.checkpoint")

VERSION = 1
_FINDING_ID = re.compile(r"^f-(\d+)$")


def finding_to_dict(f: Finding) -> dict:
    d = dataclasses.asdict(f)
    d["anchor"] = dataclasses.asdict(f.anchor) if f.anchor else None
    return d


def finding_from_dict(d: dict) -> Finding:
    anchor = d.get("anchor")
    return Finding(**{**d, "anchor": Anchor(**anchor) if anchor else None})


@dataclasses.dataclass(frozen=True)
class Entry:
    """One completed call: what it found (or tagged), what it cost, and
    whether the provider actually answered. `ok=False` entries are kept for
    the usage they burned, but a resume retries them — a failed call and a
    call that found nothing must not be confused."""
    items: list[dict]
    usage: dict
    ok: bool


class Checkpoint:
    """Completed results for one job, one file, atomic per write.

    Deliberately shaped like the batch manifest (versioned, hard on version
    mismatch, forgiving of unknown keys): the batch pipeline is the existing
    proof that (pass, chunk)-keyed results reassemble into exactly what a
    synchronous run produces."""

    def __init__(self, path: str | Path, *, fingerprint: dict):
        self.path = Path(path)
        self.fingerprint = {"version": VERSION, **fingerprint}
        self._entries: dict[str, Entry] = {}

    # -- lifecycle ------------------------------------------------------------

    def load(self) -> int:
        """Read what a previous attempt saved. Returns the number of usable
        entries; anything unreadable or stale is discarded, because a wrong
        cached answer costs correctness where a missing one only costs money."""
        self._entries = {}
        if not self.path.is_file():
            return 0
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Unreadable checkpoint %s (%s); starting clean.",
                        self.path, e)
            self.delete()
            return 0
        if raw.get("fingerprint") != self.fingerprint:
            log.info("Checkpoint at %s was made from a different document, "
                     "model or prompt set; starting clean.", self.path)
            self.delete()
            return 0
        for key, entry in (raw.get("entries") or {}).items():
            try:
                self._entries[key] = Entry(items=list(entry["items"]),
                                           usage=dict(entry["usage"]),
                                           ok=bool(entry["ok"]))
            except (KeyError, TypeError) as e:
                log.warning("Skipping malformed checkpoint entry %s (%s)",
                            key, e)
        done = sum(1 for e in self._entries.values() if e.ok)
        if done:
            log.info("Resuming: %d of the calls this run needs were already "
                     "paid for.", done)
        return done

    def get(self, key: str) -> Entry | None:
        """A completed call, or None. Failed calls answer None on purpose —
        the resume's job is to retry them, not to trust them."""
        entry = self._entries.get(key)
        return entry if entry is not None and entry.ok else None

    def burned(self, key: str) -> dict | None:
        """The usage a failed earlier attempt at this call already paid, or
        None. The retry cannot recover those tokens, but the totals must
        still count them — this is the other half of keeping ok=False
        entries at all."""
        entry = self._entries.get(key)
        return entry.usage if entry is not None and not entry.ok else None

    def put(self, key: str, *, items: list[dict], usage: Usage,
            ok: bool) -> None:
        self._entries[key] = Entry(items=items,
                                   usage=dataclasses.asdict(usage), ok=ok)
        self._write()

    def delete(self) -> None:
        self._entries = {}
        self.path.unlink(missing_ok=True)

    # -- what a resume needs to know ------------------------------------------

    def max_finding_id(self) -> int:
        """The highest f-NNNN handed out so far, so the shared counter resumes
        past it instead of colliding with cached findings."""
        highest = 0
        for entry in self._entries.values():
            for item in entry.items:
                match = _FINDING_ID.match(str(item.get("finding_id", "")))
                if match:
                    highest = max(highest, int(match.group(1)))
        return highest

    # -- disk -----------------------------------------------------------------

    def _write(self) -> None:
        body = json.dumps({
            "fingerprint": self.fingerprint,
            "entries": {k: dataclasses.asdict(e)
                        for k, e in self._entries.items()},
        }, indent=2)
        # Atomic, because this file's whole reason to exist is surviving the
        # process dying at an arbitrary moment.
        staging = self.path.with_name(self.path.name + ".writing")
        staging.write_text(body, encoding="utf-8")
        os.replace(staging, self.path)


def usage_delta(before: Usage, after: Usage) -> Usage:
    """What one call added to a running total, as its own Usage."""
    return Usage(**{f.name: getattr(after, f.name) - getattr(before, f.name)
                    for f in dataclasses.fields(Usage)})


def snapshot(usage: Usage) -> Usage:
    return Usage(**dataclasses.asdict(usage))


def add_usage(usage: Usage, delta: dict) -> None:
    """Fold a cached per-call delta back into a running total. Not
    `Usage.add`, which always counts one api_call — a cached delta carries its
    own count, including the zero of a call that never happened."""
    for name, value in delta.items():
        setattr(usage, name, getattr(usage, name, 0) + int(value or 0))
