"""A whole-run findings checkpoint: the paid reads, saved before finish().

``run_sync`` does the expensive detector work — every typed pass, LanguageTool,
Sapling, the chapter sweep — and hands ``finish`` a findings list. ``finish``
then runs its own late stages (repair, smoothing, chapter continuity, the judge
gates) and only writes ``findings.json`` at the very end. So a crash anywhere in
``finish`` used to lose every paid read: the synchronous CLI kept nothing between
the reads completing and the deliverable being written, and the run that died in
``finish`` bought all its detector passes again on the next attempt.

This module closes that window. :func:`save` writes the assembled findings, the
usage, and the coverage ledger to one JSON file the instant ``run_sync`` returns,
before ``finish`` is called; :func:`load` reads it back on a ``--resume`` so a
rerun skips the reads entirely and goes straight to ``finish``. The fingerprint —
the document's content hash, the model, the config — is what stops a stale
checkpoint from being replayed onto a changed manuscript: a mismatch is
discarded, never trusted.

Distinct from :mod:`docproof.checkpoint`, which caches individual (pass, chunk)
calls *within* ``run_sync`` to make that stage resumable. This one makes the
*boundary* between ``run_sync`` and ``finish`` survivable. The app path wires the
per-call checkpoint; the CLI writes this one.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from .checkpoint import finding_from_dict, finding_to_dict
from .models import CoverageGap, CoverageLedger, StageWarning, Usage
from .utils.files import write_atomic
from .windowing import WindowReport

log = logging.getLogger("docproof.run_checkpoint")

VERSION = 1
FILENAME = "findings.checkpoint.json"

_WINDOW_FIELDS = ("label", "asked", "answered", "truncated_calls", "extra_calls")


@dataclasses.dataclass(frozen=True)
class Resume:
    """What a resumed run reads back in place of the paid detector pass."""

    findings: list
    usage: Usage
    coverage: CoverageLedger


# --- coverage (de)serialization ----------------------------------------------
# finish() only writes to the coverage ledger; the report writers read it at the
# end. Carrying it means a resumed run's summary still names the gaps and
# degraded passes the original reads found, instead of reading falsely clean.

def _coverage_to_dict(cov: CoverageLedger | None) -> dict[str, Any] | None:
    if cov is None:
        return None
    return {
        "total": cov.total,
        "gaps": [dataclasses.asdict(g) for g in cov.gaps],
        "unruled": [dataclasses.asdict(w) for w in cov.unruled],
        "degraded": [dataclasses.asdict(s) for s in cov.degraded],
    }


def _coverage_from_dict(d: dict[str, Any] | None) -> CoverageLedger:
    cov = CoverageLedger()
    if not d:
        return cov
    cov.total = int(d.get("total", 0))
    cov.gaps = [
        CoverageGap(pass_label=str(g.get("pass_label", "")),
                    chunk_id=str(g.get("chunk_id", "")),
                    para_ids=tuple(g.get("para_ids") or ()))
        for g in (d.get("gaps") or []) if isinstance(g, dict)
    ]
    cov.unruled = [
        WindowReport(**{k: w[k] for k in _WINDOW_FIELDS if k in w})
        for w in (d.get("unruled") or []) if isinstance(w, dict)
    ]
    cov.degraded = [
        StageWarning(label=str(s.get("label", "")),
                     reason=str(s.get("reason", "")),
                     kind=str(s.get("kind", "failed")))
        for s in (d.get("degraded") or []) if isinstance(s, dict)
    ]
    return cov


# --- public API ---------------------------------------------------------------

def save(out_dir: str | Path, *, findings: list, usage: Usage,
         coverage: CoverageLedger | None, fingerprint: dict[str, Any]) -> Path:
    """Snapshot the paid reads to ``out_dir/findings.checkpoint.json``, atomically.

    Called the instant ``run_sync`` returns, so whatever ``finish`` does next, the
    detector passes are already on disk and a ``--resume`` can replay them.
    """

    path = Path(out_dir) / FILENAME
    payload = {
        "version": VERSION,
        "fingerprint": fingerprint,
        "usage": dataclasses.asdict(usage),
        "coverage": _coverage_to_dict(coverage),
        "findings": [finding_to_dict(f) for f in findings],
    }
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))
    log.info("Findings checkpoint written: %d finding(s) safe before finish() "
             "(%s)", len(findings), path)
    return path


def load(out_dir: str | Path, *, fingerprint: dict[str, Any]) -> Resume | None:
    """Read a checkpoint back, or ``None`` if there isn't a usable one.

    ``None`` covers every reason not to trust it — missing, unreadable, a version
    or fingerprint mismatch (a different document, model, or config) — because a
    wrong replay costs correctness where a missing one only costs money.
    """

    path = Path(out_dir) / FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Unreadable findings checkpoint %s (%s); ignoring.", path, e)
        return None
    if (not isinstance(payload, dict)
            or payload.get("version") != VERSION
            or payload.get("fingerprint") != fingerprint):
        log.info("Findings checkpoint at %s is stale (different document, model, "
                 "or config); ignoring it and reviewing fresh.", path)
        return None
    try:
        findings = [finding_from_dict(d) for d in payload.get("findings", [])]
        usage = Usage(**payload.get("usage", {}))
        coverage = _coverage_from_dict(payload.get("coverage"))
    except (TypeError, KeyError, ValueError) as e:
        log.warning("Malformed findings checkpoint %s (%s); ignoring.", path, e)
        return None
    log.info("Resuming from findings checkpoint: %d finding(s); skipping the "
             "paid reads.", len(findings))
    return Resume(findings=findings, usage=usage, coverage=coverage)


def clear(out_dir: str | Path) -> None:
    """Remove the checkpoint once ``finish`` has written the real deliverable, so
    it can never shadow a later, different run."""

    Path(out_dir, FILENAME).unlink(missing_ok=True)


__all__ = ["Resume", "FILENAME", "save", "load", "clear"]
