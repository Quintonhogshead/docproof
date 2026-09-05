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

The file is append-only: a fingerprint header, then one line per completed
call. It used to be one JSON document rewritten in full after every call, which
made a resumable review quadratic in its own results — a 400-page book is ~450
calls, and the last of them rewrote every finding the first 449 had produced,
around 250 MB of writes for 1 MB of state, all of it on the thread folding
results in. Appending costs one line each.

Two things follow from the format, both improvements. A crash mid-write can
only damage the final line, and `load` drops that one line instead of the whole
file — where the old whole-document parse gave up everything for one bad byte.
And a checkpoint written by the previous format no longer parses, so it is
discarded and that run starts clean: a one-time cost, paid only by a review
that was mid-flight at the moment of the upgrade.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from pathlib import Path

from .models import Anchor, Finding, Usage

log = logging.getLogger("docproof.checkpoint")

VERSION = 1
_FINDING_ID = re.compile(r"^f-(\d+)$")


def _parse(line: str) -> dict | None:
    """One JSON line, or None if it is not one. A half-written final line is the
    expected reason, not an exceptional one."""
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return row if isinstance(row, dict) else None


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
    # Optional shadow-lane receipts associated with this paid call. Unknown to
    # older checkpoints and ignored by consumers that do not need them.
    metadata: dict = dataclasses.field(default_factory=dict)


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


    def load(self) -> int:
        """Read what a previous attempt saved. Returns the number of usable
        entries; anything unreadable or stale is discarded, because a wrong
        cached answer costs correctness where a missing one only costs money.

        Lines are applied in file order into one dict, so a key written twice —
        a call that failed, then succeeded on a resume — ends up at its latest
        answer, and `max_finding_id` never sees ids the superseded attempt
        handed out."""
        self._entries = {}
        if not self.path.is_file():
            return 0
        try:
            lines = self.path.read_text("utf-8").splitlines()
        except OSError as e:
            log.warning("Unreadable checkpoint %s (%s); starting clean.",
                        self.path, e)
            self.delete()
            return 0
        header = _parse(lines[0]) if lines else None
        if header is None or header.get("fingerprint") != self.fingerprint:
            log.info("Checkpoint at %s is unreadable, or was made from a "
                     "different document, model or prompt set; starting clean.",
                     self.path)
            self.delete()
            return 0
        for n, line in enumerate(lines[1:], start=2):
            if not line.strip():
                continue
            row = _parse(line)
            if row is None:
                # Almost always the last line of a file whose process died
                # mid-write. Everything before it is intact and paid for.
                log.warning("Dropping unreadable checkpoint line %d of %s; "
                            "the calls before it still count.", n, self.path)
                continue
            try:
                self._entries[row["key"]] = Entry(
                    items=list(row["items"]), usage=dict(row["usage"]),
                    ok=bool(row["ok"]), metadata=dict(row.get("metadata") or {}))
            except (KeyError, TypeError) as e:
                log.warning("Skipping malformed checkpoint entry on line %d "
                            "(%s)", n, e)
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
            ok: bool, metadata: dict | None = None) -> None:
        entry = Entry(items=items, usage=dataclasses.asdict(usage), ok=ok,
                      metadata=dict(metadata or {}))
        self._entries[key] = entry
        self._append(key, entry)

    def delete(self) -> None:
        self._entries = {}
        self.path.unlink(missing_ok=True)


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


    def _append(self, key: str, entry: Entry) -> None:
        """One completed call, one line.

        The header is re-emitted whenever the file is missing or empty, which is
        both the first write of a run and the repair for a file deleted out from
        under a run still folding — cancelling a job unlinks it from another
        thread. Without that, the next append would build a headerless file that
        the next `load` could only throw away.

        A file whose last line has no newline was torn by a process that died
        mid-write. `load` drops that fragment but leaves it on disk, so this
        terminates it before appending: writing straight onto it would splice the
        fragment and the new entry into one unparseable line, and the call being
        recorded — already paid for — would be dropped by every later load and
        bought again on every resume."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        size = self._size()
        with open(self.path, "a", encoding="utf-8") as fh:
            if not size:
                fh.write(json.dumps({"fingerprint": self.fingerprint}) + "\n")
            elif not self._ends_cleanly():
                fh.write("\n")
            fh.write(json.dumps({"key": key, **dataclasses.asdict(entry)}) + "\n")

    def _size(self) -> int:
        """Bytes on disk, or 0 for a file that is missing — including one
        unlinked between the check and the stat, which is the cancel this
        method's docstring describes and must not raise into the run."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _ends_cleanly(self) -> bool:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)
                return fh.read(1) == b"\n"
        except OSError:
            return True                              # unreadable: don't add noise


def usage_delta(before: Usage, after: Usage) -> Usage:
    """What one call added to a running total, as its own Usage. The scalar
    counters subtract directly; `by_model` is a nested dict, so its delta is the
    per-model, per-field difference — the same shape, carried so a resumed run
    replays the call's model attribution and not just its token totals."""
    d = Usage()
    for f in dataclasses.fields(Usage):
        if f.name == "by_model":
            continue
        setattr(d, f.name, getattr(after, f.name) - getattr(before, f.name))
    bm: dict = {}
    for model, tk in after.by_model.items():
        prev = before.by_model.get(model, {})
        diff = {k: tk.get(k, 0) - prev.get(k, 0) for k in tk}
        if any(diff.values()):
            bm[model] = diff
    d.by_model = bm
    return d


def snapshot(usage: Usage) -> Usage:
    return Usage(**dataclasses.asdict(usage))     # asdict deep-copies by_model


def add_usage(usage: Usage, delta: dict) -> None:
    """Fold a cached per-call delta back into a running total. Not
    `Usage.add`, which always counts one api_call — a cached delta carries its
    own count, including the zero of a call that never happened."""
    for name, value in delta.items():
        if name == "by_model":
            for model, tk in (value or {}).items():
                bucket = usage.by_model.setdefault(model, {})
                for k, v in tk.items():
                    bucket[k] = bucket.get(k, 0) + int(v or 0)
            continue
        setattr(usage, name, getattr(usage, name, 0) + int(value or 0))
