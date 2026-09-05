"""Ingest precedent rulings from archived jobs and case files.

Archived corrections, reviews, and case files are projected into the durable
:class:`~galley.memory.store.MemoryStore` ``precedents`` table.

The reader accepts corrections resolutions, review dispositions, and case files;
case-file arbitration bookkeeping is excluded.

Malformed records are logged and skipped; valid records in the batch continue.
Inserts are idempotent using a stable key over
``(error_type, find_text, ruling, book, ruled_by)``.
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
    # galley's own adjudication vocabulary (galley.contracts.RULINGS): "keep"
    # is that vocabulary's "this finding held its span/passed the panel", the
    # same disposition "accept"/"applied" name for corrections and review.
    "keep": "accept",
    # DocProof's findings.json: "validated" is an applied tracked change, and
    # "rejected_by_verifier" the one rejection that is an editorial judgment.
    "validated": "accept",
    "rejected_by_verifier": "reject",
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

# DocProof statuses that are bookkeeping, not rulings: a candidate the
# validator could not anchor, a mechanical overlap/duplicate/no-op drop, an
# oversize guard, or a row never adjudicated. None says whether the edit was
# right, so none is a precedent — ignored quietly, not warned about.
_NOT_A_RULING: frozenset[str] = frozenset({
    "pending",
    "rejected_no_anchor",
    "rejected_overlap",
    "rejected_duplicate",
    "rejected_noop",
    "rejected_oversized",
    "skipped_low_confidence",
})

# The arbitrator's verdicts (galley.adjudicate.arbitrate) record which finding
# claimed a span — an overlap loser routed to query, a re-find merged as a
# duplicate. Neither is a judgment on the mark, so neither is a precedent.
_ARBITRATION_JUDGE = "arbitrator"
_ARBITRATION_REASONS = ("overlaps earlier finding", "duplicate of ")


def _is_arbitration(item: dict[str, Any]) -> bool:
    """Is this review item an arbitration verdict rather than a ruling?"""
    judge = _first(item, "judge", "ruled_by").strip().lower()
    if judge == _ARBITRATION_JUDGE:
        return True
    reason = _first(item, "reason", "note", "comment", "why").strip().lower()
    return reason.startswith(_ARBITRATION_REASONS)


def _normalize_ruling(raw: Any) -> str | None:
    """Map a raw disposition string to the canonical vocabulary, or ``None`` if
    it is missing / unrecognized (caller logs and skips the finding)."""
    if not isinstance(raw, str):
        return None
    return _RULING_NORMALIZATION.get(raw.strip().lower())




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
    # Well-formed rows that are deliberately not precedents: arbitration
    # bookkeeping and DocProof's mechanical statuses (``_NOT_A_RULING``).
    ignored: int = 0

    def _add(self, other: "IngestSummary") -> None:
        self.ingested += other.ingested
        self.duplicates += other.duplicates
        self.skipped += other.skipped
        self.ignored += other.ignored




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
    now: Any = None,
) -> None:
    """Insert one precedent unless an equal row already exists (in the store from
    a prior run, or already inserted this batch). A ruling outside
    :data:`RULING_VOCAB` is refused here — the one gate every shape passes
    through — so the table never grows a vocabulary of its own."""
    if ruling not in RULING_VOCAB:
        logger.warning("precedent ruling %r is outside %s; skipping",
                       ruling, sorted(RULING_VOCAB))
        summary.skipped += 1
        return
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
        now=_now_text(now),
    )
    seen.add(key)
    summary.ingested += 1


def _now_text(now: Any) -> str | None:
    """``now`` as the timestamp string the store pins ``created_at`` to, or
    ``None`` to let the store's own clock date the row."""
    if now is None:
        return None
    if callable(now):
        now = now()
    return str(now)



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




def _ingest_corrections(
    store: MemoryStore,
    job: dict[str, Any],
    *,
    book: str,
    seen: set[str],
    summary: IngestSummary,
    now: Any = None,
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
            now=now,
        )


def _ingest_review(
    store: MemoryStore,
    job: dict[str, Any],
    *,
    book: str,
    seen: set[str],
    summary: IngestSummary,
    now: Any = None,
) -> None:
    job_ruled_by = _first(job, "ruled_by", "reviewer", "editor", default="review")
    for item in _pluck_list(job, _REVIEW_LIST_KEYS):
        if not isinstance(item, dict):
            logger.warning("review finding is not an object; skipping: %r", item)
            summary.skipped += 1
            continue
        if _is_arbitration(item):
            logger.debug("review finding is arbitration bookkeeping; ignoring: %r",
                         item.get("reason"))
            summary.ignored += 1
            continue
        raw = (
            item.get("disposition")
            if item.get("disposition") is not None
            else item.get("status")
            if item.get("status") is not None
            else item.get("ruling")
        )
        if isinstance(raw, str) and raw.strip().lower() in _NOT_A_RULING:
            logger.debug("review finding status %r is not a ruling; ignoring", raw)
            summary.ignored += 1
            continue
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
        find_text = _first(
            item, "find_text", "find", "original_text", "text", "original", "before"
        )
        ruled_by = _first(
            item, "ruled_by", "judge", "reviewer", "editor", default=job_ruled_by
        )
        reason = _first(item, "reason", "explanation", "note", "comment", "why")
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
            now=now,
        )




def ingest_job(
    store: MemoryStore,
    job_json: dict[str, Any],
    *,
    now: Any = None,
    book: str | None = None,
    seen: set[str] | None = None,
) -> IngestSummary:
    """Ingest one archived job record into ``store``'s precedents.

    Detects whether ``job_json`` is a corrections-resolutions record or a review
    record (see :func:`_detect_shape`) and inserts the precedents it can extract.
    An unrecognized record is logged and skipped (returns ``skipped=1``), never
    raised. ``book`` is the fallback book label when the record carries none
    (:func:`ingest_archive` passes the file stem). ``seen`` lets a batch share one
    dedup set across jobs; ``now`` (a timestamp string, or a callable returning
    one) dates every row this call writes — omit it and the store's own clock
    does.
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
        _ingest_corrections(store, job_json, book=book_label, seen=seen,
                            summary=summary, now=now)
    else:
        _ingest_review(store, job_json, book=book_label, seen=seen,
                       summary=summary, now=now)
    return summary


def casefile_items(cf: Any) -> list[dict[str, Any]]:
    """Project a :class:`~galley.casefile.CaseFile` into review-shape items.

    Every finding yields one item: its verdict's ruling when it was adjudicated
    (``keep`` -> accept, ``reject``, ``query``), or — for a finding that holds
    its span with no verdict at all, the common case for wave one — an
    ``accept`` ruled by ``galley:uncontested``, because that edit was delivered
    to the author. A finding that declares itself a query (the convention
    ``galley.letter`` and ``galley.deliverable`` honour) is a ``query``.
    Arbitration verdicts are carried through as-is and dropped by
    :func:`ingest_job`'s arbitration guard.
    """
    by_id: dict[str, Any] = {}
    for v in cf.verdicts:
        by_id.setdefault(v.finding_id, v)
    items: list[dict[str, Any]] = []
    for f in cf.findings:
        v = by_id.get(f.id)
        if v is not None:
            items.append({
                "disposition": v.ruling,
                "error_type": f.error_type or "unknown",
                "find_text": f.find,
                "reason": v.reason or f.note,
                "ruled_by": v.judge or "galley",
            })
            continue
        self_query = f.confidence == "query" or f.error_type == "query"
        items.append({
            "disposition": "query" if self_query else "accept",
            "error_type": f.error_type or "unknown",
            "find_text": f.find,
            "reason": f.note,
            "ruled_by": "galley:uncontested",
        })
    return items


def ingest_casefile(
    store: MemoryStore,
    cf: Any,
    *,
    now: Any = None,
    seen: set[str] | None = None,
) -> IngestSummary:
    """Ingest a finished run's :class:`~galley.casefile.CaseFile` as precedents.

    The case-file form of :func:`ingest_job`: see :func:`casefile_items` for
    what becomes a precedent (adjudicated findings by their verdict, uncontested
    findings as ``accept``) and what never does (arbitration bookkeeping).
    """
    job_json = {"kind": "review", "book": cf.book, "findings": casefile_items(cf)}
    return ingest_job(store, job_json, now=now, book=cf.book, seen=seen)


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
    "casefile_items",
    "ingest_archive",
    "ingest_casefile",
    "ingest_job",
]
