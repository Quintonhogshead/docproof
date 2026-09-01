"""The full-ladder adapter — DocProof's whole pipeline as wave one.

Wraps ``prepare -> run_sync -> finish`` in a wave-keyed run directory and
converts DocProof's ``Finding`` objects (as written to ``findings.json``, anchors
resolved) into Galley :class:`GFinding`s with provenance filled. The coverage
ledger's gaps / unruled / degraded passes surface as ``coverage_notes`` so the
orchestrator records DocProof's honest holes rather than mistaking silence for a
clean sweep.

Read-only with respect to the docproof package: it calls the pipeline as a
library and writes only into its own run directory. That directory is stable
(``<workspace>/wave<N>_ladder/``, the case file's folder in production) and
holds DocProof's own ``checkpoint.json``, so a wave interrupted mid-ladder
replays the reads it already paid for on resume instead of buying them twice.

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

from docproof.batch import pass_prompts
from docproof.checkpoint import Checkpoint
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


# The ``status`` values a finding may carry into the union. "validated" is an
# applied tracked change; "query" is an anchored question (the validator's
# query channel, and the verifier's / meaning gate's force_query downgrades
# land there too) that rides as a margin comment. Everything else — rejected,
# skipped, unanchored — is dropped and counted.
QUERY_STATUSES = frozenset({"query"})


def gfindings_from_json(
    findings_json: str | Path, *, wave: int, model: str
) -> tuple[list[GFinding], int]:
    """Convert a run's ``findings.json`` into GFindings.

    Findings the validator both anchored AND kept as a tracked change
    (``status == "validated"``) carry a usable, applied fix and convert
    losslessly on span, error type, and fix text. An anchored "query" row is
    kept too, as a GFinding with ``confidence="query"`` and an empty
    ``replace`` — the validator's query anchor has no insert text (a question
    is not a correction), and ``galley.deliverable`` turns that confidence
    into a force_query margin comment, so the author still sees the question.
    A "skipped_low_confidence" or rejected row has an Anchor as well but no
    real fix, and converting it would hand the case file a fabricated
    deletion; those, and unanchored rows, are counted and returned as the
    second element so the caller can note the loss.
    """

    payload = json.loads(Path(findings_json).read_text(encoding="utf-8"))
    out: list[GFinding] = []
    dropped = 0
    for f in payload.get("findings", []):
        anchor = f.get("anchor")
        status = f.get("status")
        if not anchor or status not in ("validated", *QUERY_STATUSES):
            dropped += 1
            continue
        query = status in QUERY_STATUSES
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
                replace="" if query else str(anchor.get("insert_text", "")),
                note=str(f.get("explanation", "")),
                confidence="query" if query else str(f.get("confidence", "medium")),
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
    (real or fake). Each ``run`` works in ``<workspace>/wave<N>_ladder/`` — the
    orchestrator sets ``workspace`` to the case file's directory and ``wave``
    to the wave it is running — where DocProof's checkpoint is fingerprinted
    on the document, config, and prompts, so a re-run of the same wave replays
    its paid reads and a changed prompt set starts clean on its own.

    ``calibration`` (a ``galley.calibration.Calibration``) lets the adapter
    answer the orchestrator's pre-flight ``estimate_usd`` from the observed
    ``$/kword`` of earlier ladder runs; without one it has no honest number
    and says so with ``None``.
    """

    source_path: str | Path
    cfg: Config
    provider: Any
    wave: int = 1
    workspace: str | Path | None = None
    error_dir: str | Path = DEFAULT_ERROR_DIR
    calibration: Any = None
    name: str = "docproof_ladder"

    def _run_dir(self) -> Path:
        if self.workspace is not None:
            run_dir = Path(self.workspace) / f"wave{self.wave}_ladder"
            run_dir.mkdir(parents=True, exist_ok=True)
            return run_dir
        return Path(tempfile.mkdtemp(prefix="galley-ladder-"))

    def _checkpoint(self, prepared: Any, run_dir: Path) -> Checkpoint:
        """The record of what this wave's ladder has already paid for.

        Fingerprinted the way the app's review checkpoint is — document text,
        full config, the exact prompts — because cached answers are only
        reusable while all of those are unchanged; ``load()`` wipes a stale
        one itself.
        """

        fingerprint = {
            "kind": "review",
            "content_hash": prepared.content_hash,
            "config": self.cfg.model_dump(mode="json"),
            "prompts": pass_prompts(self.cfg, prepared),
            "selection": None,
        }
        checkpoint = Checkpoint(run_dir / "checkpoint.json",
                                fingerprint=fingerprint)
        checkpoint.load()
        return checkpoint

    def estimate_usd(self, ms: Manuscript, scope: Scope) -> float | None:
        """A pre-flight price for reading ``scope`` (the whole book, for the
        ladder), from the calibrated ladder rate; ``None`` when uncalibrated."""

        if self.calibration is None:
            return None
        from galley.calibration import est_usd_per_kword

        rate = est_usd_per_kword(self.calibration, self.name,
                                 self.cfg.api.model, None)
        if rate is None:
            return None
        words = sum(len(ms.paragraphs.get(pid, "").split())
                    for pid in scope.paragraph_ids(ms))
        return (words / 1000.0) * rate

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        run_dir = self._run_dir()

        prepared = prepare(self.cfg, str(self.source_path), self.error_dir)
        coverage = CoverageLedger()
        checkpoint = self._checkpoint(prepared, run_dir)
        findings, run_usage = run_sync(
            self.cfg, prepared, self.provider, coverage=coverage,
            checkpoint=checkpoint,
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
        queries = sum(1 for g in gfindings if g.confidence == "query")
        if queries:
            notes.append(
                f"{queries} finding(s) carried as queries (margin comments, "
                f"not tracked changes)"
            )
        if dropped:
            notes.append(
                f"{dropped} finding(s) had no anchor or no applied fix and were "
                f"dropped from the union"
            )

        cost = (cost_of_usage(run_usage, fallback_model=self.cfg.api.model) or 0.0)
        cost += run_usage.sapling_cost

        return AdapterResult(findings=gfindings, coverage_notes=notes, cost_usd=cost)


__all__ = [
    "DEFAULT_ERROR_DIR",
    "DocproofLadderAdapter",
    "QUERY_STATUSES",
    "gfindings_from_json",
]
