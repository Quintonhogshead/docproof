"""G2 — Precedent ingest from archived jobs.

Runs are disposable; memory compounds. This module is the bridge from Galley's
*archived jobs* (finished corrections and review records left on disk) into the
durable :class:`~galley.memory.store.MemoryStore` ``precedents`` table, so that
how a mark was ruled on one book becomes a precedent the next book can lean on.

Two archive shapes matter, and the reader is deliberately *tolerant* — the exact
on-disk schema is broad and has drifted across job kinds, so we detect the shape
and pull what we can rather than demanding one rigid layout:

* **corrections resolutions** — a finished corrections job where a human
  accepted / dismissed / swapped each flagged mark. Each resolved flag is a
  precedent.
* **review dispositions** — a finished review job whose findings each carry an
  applied / queried / rejected disposition. Each finding is a precedent.

Contract
--------

* **Never fatal.** An unparseable file, a record of an unknown shape, or a single
  malformed finding is *logged and skipped* (:mod:`logging`); the good records in
  the same batch still ingest.
* **Idempotent.** Re-running ingest over the same archives creates no duplicate
  rows. The store's ``add_precedent`` is not itself idempotent, so dedup lives
  here: before inserting we compute a stable key over
  ``(error_type, find_text, ruling, book, ruled_by)`` and skip it if an equal row
  already exists (queried from the store) or was already inserted this batch.
* **stdlib only.** :mod:`json`, :mod:`logging`, :mod:`hashlib`, :mod:`pathlib`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from galley.memory.store import MemoryStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ruling vocabulary
# ---------------------------------------------------------------------------

# The canonical, deliberately *small* ruling vocabulary a precedent row stores.
# Matches the Precedent dataclass's documented set (accept | reject | downgrade |
# query). Every raw disposition an archive can carry normalizes into one of these.
RULING_VOCAB: frozenset[str] = frozenset({"accept", "reject", "downgrade", "query"})

# Raw disposition (lower-cased) -> canonical ruling.
#   * corrections: accept / dismiss / swap / reject
#   * review:      applied / queried / rejected / downgraded
# A "swap" (author supplied a replacement word) is an *accepted* change with a
# substitution; we normalize it to ``accept`` and preserve the "swap" detail in
# the precedent's reason rather than growing the vocabulary. (Noted choice.)
_RULING_NORMALIZATION: dict[str, str] = {
    # accept family
    "accept": "accept",
    "accepted": "accept",
    "apply": "accept",
    "applied": "accept",
    "applied_change": "accept",
    "swap": "accept",
    "swapped": "accept",
    "replace": "accept",
    "replaced": "accept",
    "substitute": "accept",
    # reject family (a dismissed mark == the proofreader's mark was rejected)
    "reject": "reject",
    "rejected": "reject",
    "dismiss": "reject",
    "dismissed": "reject",
    "decline": "reject",
    "declined": "reject",
    "ignore": "reject",
    "ignored": "reject",
    # downgrade family
    "downgrade": "downgrade",
    "downgraded": "downgrade",
    # query family
    "query": "query",
    "queried": "query",
    "queries": "query",
    "flag": "query",
    "flagged": "query",
}

# Raw dispositions we treat as "swap" so we can annotate the reason.
_SWAP_RAW: frozenset[str] = frozenset(
    {"swap", "swapped", "replace", "replaced", "substitute"}
)


def _normalize_ruling(raw: Any) -> str | None:
    """Map a raw disposition string to the canonical vocabulary, or ``None`` if
    it is missing / unrecognized (caller logs and skips the finding)."""
    if not isinstance(raw, str):
        return None
    return _RULING_NORMALIZATION.get(raw.strip().lower())


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class IngestSummary:
    """Counts from an ingest pass.

    ``ingested`` — new precedent rows written.
    ``duplicates`` — rows already present, skipped for idempotence.
    ``skipped`` — records / findings that could not be parsed (unknown shape,
    unreadable JSON, missing ruling). The required ``(ingested, skipped)`` pair
    the ticket asks for are these two; ``duplicates`` is broken out so a re-run
    (ingested == 0, duplicates == N) is distinguishable from a bad batch.
    """

    ingested: int = 0
    duplicates: int = 0
    skipped: int = 0

    def _add(self, other: "IngestSummary") -> None:
        self.ingested += other.ingested
        self.duplicates += other.duplicates
        self.skipped += other.skipped


# ---------------------------------------------------------------------------
# Field plucking — tolerant of naming drift
# ---------------------------------------------------------------------------


def _first(d: dict[str, Any], *keys: str, default: str = "") -> str:
    """First present, non-empty, stringifiable value among ``keys``."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return str(d[k])
    return default


def _book_of(job: dict[str, Any], book_hint: str | None) -> str:
    """The book label for every precedent from this job: an explicit field wins,
    else the caller's hint (e.g. the archive's file stem), else empty."""
    book = _first(job, "book", "title", "job", "job_title", "manuscript", "filename")
    if book:
        return book
    return book_hint or ""


# ---------------------------------------------------------------------------
# Idempotent insert
# ---------------------------------------------------------------------------


def _dedup_key(
    error_type: str, find_text: str, ruling: str, book: str, ruled_by: str
) -> str:
    """A stable hash over the precedent's identifying tuple. Stable across
    processes and runs (unlike :func:`hash`), so re-ingest recognizes a row it
    already wrote."""
    joined = "\x00".join((error_type, find_text, ruling, book, ruled_by))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _existing_keys(store: MemoryStore, error_type: str) -> set[str]:
    """Dedup keys already in the store for one error type."""
    keys: set[str] = set()
    for p in store.precedents(error_type=error_type):
        keys.add(_dedup_key(p.error_type, p.find_text, p.ruling, p.book, p.ruled_by))
    return keys


def _insert_precedent(
    store: MemoryStore,
    *,
    error_type: str,
    find_text: str,
    ruling: str,
    reason: str,
    book: str,
    ruled_by: str,
    seen: set[str],
    summary: IngestSummary,
) -> None:
    """Insert one precedent unless an equal row already exists (in the store from
    a prior run, or already inserted this batch)."""
    key = _dedup_key(error_type, find_text, ruling, book, ruled_by)
    if key in seen:
        summary.duplicates += 1
        return
    if key in _existing_keys(store, error_type):
        seen.add(key)  # remember so we don't re-query it within this batch
        summary.duplicates += 1
        return
    store.add_precedent(
        error_type,
        find_text,
        ruling,
        reason=reason,
        book=book,
        ruled_by=ruled_by,
    )
    seen.add(key)
    summary.ingested += 1


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------

_CORRECTIONS_KINDS = frozenset({"corrections", "correction", "resolutions"})
_REVIEW_KINDS = frozenset({"review", "findings", "proofread"})

# Where each shape keeps its list of resolved items (tried in order).
_CORRECTIONS_LIST_KEYS = ("resolutions", "resolved", "flags", "marks")
_REVIEW_LIST_KEYS = ("findings", "dispositions", "results")


def _detect_shape(job: dict[str, Any]) -> str | None:
    """Return ``"corrections"``, ``"review"``, or ``None`` (unrecognized).

    Trust an explicit ``kind`` field first; otherwise infer from which list key
    is present (corrections wins if both, since its items are the more specific
    'resolution' shape)."""
    kind = job.get("kind")
    if isinstance(kind, str):
        k = kind.strip().lower()
        if k in _CORRECTIONS_KINDS:
            return "corrections"
        if k in _REVIEW_KINDS:
            return "review"
        # explicit but unknown kind — fall through to key sniffing
    if any(k in job for k in _CORRECTIONS_LIST_KEYS):
        return "corrections"
    if any(k in job for k in _REVIEW_LIST_KEYS):
        return "review"
    return None


def _pluck_list(job: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for k in keys:
        v = job.get(k)
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# Per-shape ingest
# ---------------------------------------------------------------------------


def _ingest_corrections(
    store: MemoryStore,
    job: dict[str, Any],
    *,
    book: str,
    seen: set[str],
    summary: IngestSummary,
) -> None:
    job_ruled_by = _first(job, "ruled_by", "resolved_by", "editor", "reviewer")
    for item in _pluck_list(job, _CORRECTIONS_LIST_KEYS):
        if not isinstance(item, dict):
            logger.warning("corrections resolution is not an object; skipping: %r", item)
            summary.skipped += 1
            continue
        raw = (
            item.get("resolution")
            if item.get("resolution") is not None
            else item.get("disposition")
            if item.get("disposition") is not None
            else item.get("ruling")
        )
        ruling = _normalize_ruling(raw)
        if ruling is None:
            logger.warning(
                "corrections resolution has no recognizable ruling (%r); skipping",
                raw,
            )
            summary.skipped += 1
            continue
        error_type = _first(
            item, "error_type", "type", "category", "kind", default="unknown"
        )
        find_text = _first(
            item, "find_text", "find", "text", "original", "mark", "before"
        )
        ruled_by = _first(
            item, "ruled_by", "resolved_by", "author", "editor", default=job_ruled_by
        )
        reason = _first(item, "reason", "note", "comment", "why")
        if isinstance(raw, str) and raw.strip().lower() in _SWAP_RAW:
            replacement = _first(item, "to", "after", "replacement", "swap_to")
            detail = f"swap -> {replacement}" if replacement else "swap"
            reason = f"{reason}; {detail}" if reason else detail
        _insert_precedent(
            store,
            error_type=error_type,
            find_text=find_text,
            ruling=ruling,
            reason=reason,
            book=book,
            ruled_by=ruled_by,
            seen=seen,
            summary=summary,
        )


def _ingest_review(
    store: MemoryStore,
    job: dict[str, Any],
    *,
    book: str,
    seen: set[str],
    summary: IngestSummary,
) -> None:
    job_ruled_by = _first(job, "ruled_by", "reviewer", "editor", default="review")
    for item in _pluck_list(job, _REVIEW_LIST_KEYS):
        if not isinstance(item, dict):
            logger.warning("review finding is not an object; skipping: %r", item)
            summary.skipped += 1
            continue
        raw = (
            item.get("disposition")
            if item.get("disposition") is not None
            else item.get("status")
            if item.get("status") is not None
            else item.get("ruling")
        )
        ruling = _normalize_ruling(raw)
        if ruling is None:
            logger.warning(
                "review finding has no recognizable disposition (%r); skipping", raw
            )
            summary.skipped += 1
            continue
        error_type = _first(
            item, "error_type", "type", "category", "kind", default="unknown"
        )
        find_text = _first(item, "find_text", "find", "text", "original", "before")
        ruled_by = _first(item, "ruled_by", "reviewer", "editor", default=job_ruled_by)
        reason = _first(item, "reason", "note", "comment", "why")
        _insert_precedent(
            store,
            error_type=error_type,
            find_text=find_text,
            ruling=ruling,
            reason=reason,
            book=book,
            ruled_by=ruled_by,
            seen=seen,
            summary=summary,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_job(
    store: MemoryStore,
    job_json: dict[str, Any],
    *,
    now: Any = None,  # accepted for symmetry / future use; the store owns the clock
    book: str | None = None,
    seen: set[str] | None = None,
) -> IngestSummary:
    """Ingest one archived job record into ``store``'s precedents.

    Detects whether ``job_json`` is a corrections-resolutions record or a review
    record (see :func:`_detect_shape`) and inserts the precedents it can extract.
    An unrecognized record is logged and skipped (returns ``skipped=1``), never
    raised. ``book`` is the fallback book label when the record carries none
    (:func:`ingest_archive` passes the file stem). ``seen`` lets a batch share one
    dedup set across jobs; ``now`` is accepted but unused — the store injects its
    own clock at write time.
    """
    summary = IngestSummary()
    if seen is None:
        seen = set()
    if not isinstance(job_json, dict):
        logger.warning("archived job is not a JSON object; skipping: %r", type(job_json))
        summary.skipped += 1
        return summary

    shape = _detect_shape(job_json)
    if shape is None:
        logger.warning(
            "unrecognized archived-job shape (kind=%r, keys=%r); skipping",
            job_json.get("kind"),
            sorted(job_json.keys()),
        )
        summary.skipped += 1
        return summary

    book_label = _book_of(job_json, book)
    if shape == "corrections":
        _ingest_corrections(store, job_json, book=book_label, seen=seen, summary=summary)
    else:
        _ingest_review(store, job_json, book=book_label, seen=seen, summary=summary)
    return summary


def ingest_archive(
    store: MemoryStore,
    paths_or_dicts: str | Path | Iterable[str | Path | dict[str, Any]],
    *,
    now: Any = None,
) -> IngestSummary:
    """Walk a batch of archived jobs into ``store``, returning combined counts.

    ``paths_or_dicts`` may be:

    * a directory path — every ``*.json`` in it (sorted) is read;
    * a single ``.json`` file path;
    * an iterable mixing file paths and already-parsed job dicts.

    Each element is handed to :func:`ingest_job`. A file that is not valid JSON,
    or any single bad record, is logged and skipped — the rest of the batch still
    ingests. A dedup set is shared across the whole batch so idempotence holds
    within one call as well as across re-runs.
    """
    total = IngestSummary()
    seen: set[str] = set()

    for entry in _iter_entries(paths_or_dicts):
        if isinstance(entry, dict):
            total._add(ingest_job(store, entry, now=now, seen=seen))
            continue
        # entry is a path to a JSON file
        path = Path(entry)
        try:
            raw = path.read_text(encoding="utf-8")
            job = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning("could not read/parse archived job %s: %s", path, exc)
            total.skipped += 1
            continue
        total._add(ingest_job(store, job, now=now, book=path.stem, seen=seen))

    return total


def _iter_entries(
    paths_or_dicts: str | Path | Iterable[str | Path | dict[str, Any]],
) -> Iterable[str | Path | dict[str, Any]]:
    """Normalize the ``ingest_archive`` argument into a flat sequence of entries
    (dicts or file paths)."""
    # A single dict — one job.
    if isinstance(paths_or_dicts, dict):
        return [paths_or_dicts]
    # A path: directory -> its *.json files; file -> itself.
    if isinstance(paths_or_dicts, (str, Path)):
        p = Path(paths_or_dicts)
        if p.is_dir():
            return sorted(p.glob("*.json"))
        return [p]
    # An iterable of paths and/or dicts.
    return list(paths_or_dicts)


__all__ = [
    "IngestSummary",
    "RULING_VOCAB",
    "ingest_archive",
    "ingest_job",
]
