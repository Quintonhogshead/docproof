"""The full-ladder adapter — DocProof's whole pipeline as wave one.

Wraps ``prepare -> run_sync -> finish`` in a fresh run directory per call and
converts DocProof's ``Finding`` objects (as written to ``findings.json``, anchors
resolved) into Galley :class:`GFinding`s with provenance filled. The coverage
ledger's gaps / unruled / degraded passes surface as ``coverage_notes`` so the
orchestrator records DocProof's honest holes rather than mistaking silence for a
clean sweep.

Read-only with respect to the docproof package: it calls the pipeline as a
library and writes only into a throwaway run directory.

The adapter holds its own ``provider`` because the :class:`DetectorAdapter`
protocol's ``run`` takes no provider — the orchestrator constructs the adapter
with one (a real ``Provider`` in production, a ``FakeProvider`` under test).
Wave one runs the whole book, so ``scope`` is advisory here; targeted re-reads
are the single-pass adapter's job (B2).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docproof.config import Config
from docproof.models import CoverageLedger, Usage
from docproof.pipeline import finish, prepare, run_sync
from docproof.providers import cost_of_usage

from galley.adapters import AdapterResult, Scope
from galley.contracts import GFinding, Manuscript, Provenance, Span

# The registry of error-type definitions the pipeline reads in prepare().
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ERROR_DIR = _REPO_ROOT / "config" / "error_types"


def _accumulate(dst: Usage, src: Usage) -> None:
    """Fold ``src``'s totals into ``dst``, buckets included.

    ``run_sync`` builds its own ``Usage`` and returns it; the orchestrator wants
    every wave's spend on one shared object, so we merge rather than replace.
    """

    dst.input_tokens += src.input_tokens
    dst.output_tokens += src.output_tokens
    dst.cache_creation_input_tokens += src.cache_creation_input_tokens
    dst.cache_read_input_tokens += src.cache_read_input_tokens
    dst.api_calls += src.api_calls
    dst.sapling_chars += src.sapling_chars
    dst.sapling_cost += src.sapling_cost
    for model, bucket in src.by_model.items():
        into = dst.by_model.setdefault(model, {})
        for key, value in bucket.items():
            into[key] = into.get(key, 0) + value


def _coverage_notes(coverage: CoverageLedger) -> list[str]:
    """Render the ledger's gaps / unruled / degraded passes as short notes."""

    notes: list[str] = []
    for gap in coverage.gaps:
        notes.append(
            f"gap: {gap.pass_label} left {len(gap.para_ids)} paragraph(s) "
            f"unreviewed (chunk {gap.chunk_id})"
        )
    for report in coverage.unruled:
        notes.append(
            f"unruled: {report.label} lost {report.lost} of {report.asked} "
            f"window(s) to truncation"
        )
    for warning in coverage.degraded:
        notes.append(f"degraded: {warning.label}: {warning.reason}")
    return notes


def gfindings_from_json(
    findings_json: str | Path, *, wave: int, model: str
) -> tuple[list[GFinding], int]:
    """Convert a run's ``findings.json`` into GFindings.

    Only findings the validator anchored carry a usable span; unanchored findings
    (unplaced) are counted and returned as the second element so the caller can
    note the loss. Conversion is lossless on span, error type, and the fix text.
    """

    payload = json.loads(Path(findings_json).read_text(encoding="utf-8"))
    out: list[GFinding] = []
    dropped = 0
    for f in payload.get("findings", []):
        anchor = f.get("anchor")
        if not anchor:
            dropped += 1
            continue
        out.append(
            GFinding(
                id=str(f.get("finding_id", "")),
                error_type=str(f.get("error_type", "")),
                span=Span(
                    para_id=str(f.get("para_id", "")),
                    start=int(anchor.get("start", 0)),
                    end=int(anchor.get("end", 0)),
                ),
                find=str(anchor.get("delete_text", "")),
                replace=str(anchor.get("insert_text", "")),
                note=str(f.get("explanation", "")),
                confidence=str(f.get("confidence", "medium")),
                provenance=Provenance(
                    detector="docproof_ladder", wave=wave, model=model, cost_usd=0.0
                ),
            )
        )
    return out, dropped


@dataclass
class DocproofLadderAdapter:
    """Runs the full DocProof ladder over the source book and returns GFindings.

    Construct with the source document path, a built ``Config``, and a ``provider``
    (real or fake). Each ``run`` uses a fresh run directory, so agent-varied
    prompts across waves never invalidate a checkpoint fingerprint.
    """

    source_path: str | Path
    cfg: Config
    provider: Any
    wave: int = 1
    workspace: str | Path | None = None
    error_dir: str | Path = DEFAULT_ERROR_DIR
    name: str = "docproof_ladder"

    def _fresh_run_dir(self) -> Path:
        if self.workspace is not None:
            base = Path(self.workspace)
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=f"wave{self.wave}-", dir=str(base)))
        return Path(tempfile.mkdtemp(prefix="galley-ladder-"))

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        run_dir = self._fresh_run_dir()

        prepared = prepare(self.cfg, str(self.source_path), self.error_dir)
        coverage = CoverageLedger()
        findings, run_usage = run_sync(
            self.cfg, prepared, self.provider, coverage=coverage
        )
        outputs = finish(
            prepared,
            findings,
            run_usage,
            self.cfg,
            out_dir=run_dir,
            source_path=self.source_path,
            coverage=coverage,
        )

        # Thread the whole run's spend onto the shared usage object.
        _accumulate(usage, run_usage)

        gfindings, dropped = gfindings_from_json(
            outputs.findings_json, wave=self.wave, model=self.cfg.api.model
        )

        notes = _coverage_notes(coverage)
        notes.extend(outputs.warnings)
        if dropped:
            notes.append(
                f"{dropped} finding(s) had no anchor and were dropped from the union"
            )

        cost = (cost_of_usage(run_usage, fallback_model=self.cfg.api.model) or 0.0)
        cost += run_usage.sapling_cost

        return AdapterResult(findings=gfindings, coverage_notes=notes, cost_usd=cost)


__all__ = [
    "DEFAULT_ERROR_DIR",
    "DocproofLadderAdapter",
    "gfindings_from_json",
]
