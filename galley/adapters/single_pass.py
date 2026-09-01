"""The single-pass adapter — one error-type group over a chosen span set.

Where :class:`DocproofLadderAdapter` runs DocProof's whole ladder over the whole
book, this adapter is the surgical instrument every later wave reaches for: send
*only* the scoped paragraphs to the model, for *only* the named error-type
group, and hand back anchored :class:`GFinding`s. A wave that suspects a missed
comma splice in chapter 3 re-reads chapter 3 for comma splices and nothing else,
at whatever model and repeat count the wave dials in.

It drives DocProof as a library — ``prepare`` (analyses off) for the ingested,
normalized document model, then the ``Analyzer`` / ``validate_findings`` pair by
hand — so the read is exactly the scoped paragraphs and no more. It is read-only
with respect to the DocProof package and the manuscript.

Like the ladder adapter it HOLDS its provider: the :class:`DetectorAdapter`
protocol's ``run`` takes no provider, so the orchestrator constructs the adapter
with one (a real ``Provider`` in production, a fake under test).

Restricting the read to a paragraph subset is done by *rechunking a filtered
document*, not by ``prepare``'s ``selection=`` — that argument filters by chunk
id, and a chunk carries neighbouring paragraphs, so it cannot express "only
these paragraphs". We keep the full ``prepared.doc`` for anchoring (the validator
needs every paragraph's canonical text) but build the chunks the model actually
sees from a document filtered to the scoped ids alone.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docproof.chunker import chunk_document
from docproof.config import Config
from docproof.models import Usage
from docproof.pipeline import build_analyzers, prepare
from docproof.providers import cost_of_usage
from docproof.validator import validate_findings

from galley.adapters import AdapterResult, Scope
from galley.adapters.docproof_ladder import DEFAULT_ERROR_DIR
from galley.contracts import GFinding, Manuscript, Provenance, Span


def _merge_usage(dst: Usage, src: Usage) -> None:
    """Fold ``src``'s totals into the shared ``dst``, per-model buckets included.

    The adapter meters its own run on a *local* ``Usage`` so it can price exactly
    this call, then merges into the orchestrator's shared object — the same
    local-then-merge shape ``docproof_ladder._accumulate`` uses.
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


@dataclass
class SinglePassAdapter:
    """One error-type group, one scoped span set, one (repeatable) read.

    Construct with the source document path, a built ``Config``, and a
    ``provider`` (real or fake). ``run`` prepares the document with analyses off,
    rechunks the scoped paragraphs, reads them for the scoped group, anchors the
    findings, and returns them as GFindings with wave provenance.
    """

    source_path: str | Path
    cfg: Config
    provider: Any
    wave: int = 1
    error_dir: str | Path = DEFAULT_ERROR_DIR
    # A ``galley.calibration.Calibration``; lets ``estimate_usd`` price a scope
    # from the observed single_pass rate. None = no honest estimate.
    calibration: Any = None
    name: str = "single_pass"

    def estimate_usd(self, ms: Manuscript, scope: Scope) -> float | None:
        """A pre-flight price for re-reading ``scope`` from the calibrated
        single_pass rate; ``None`` when nothing has been calibrated yet."""

        if self.calibration is None:
            return None
        from galley.calibration import est_usd_per_kword

        rate = est_usd_per_kword(self.calibration, self.name,
                                 scope.model or self.cfg.api.model, None)
        if rate is None:
            return None
        words = sum(len(ms.paragraphs.get(pid, "").split())
                    for pid in scope.paragraph_ids(ms))
        return (words / 1000.0) * rate * max(1, scope.passes)

    def _group_keys(self, scope: Scope) -> list[list[str]]:
        """The error-type group(s) to run, as lists of registry keys.

        ``scope.error_groups`` names the keys to read as one combined pass; empty
        falls back to the config's own groups, and a config with none defined
        falls back to a lone spelling pass so the adapter always has something to
        load.
        """

        if scope.error_groups:
            return [list(scope.error_groups)]
        groups = [list(g) for g in self.cfg.error_type_groups]
        return groups or [["spelling"]]

    def run(
        self,
        ms: Manuscript,
        scope: Scope,
        budget_usd: float,
        usage: Usage,
    ) -> AdapterResult:
        group_keys = self._group_keys(scope)

        # A working copy of the config: honour scope.model without mutating the
        # caller's config, and pin error_types to the scoped group so prepare()
        # loads exactly those prompts from error_dir.
        cfg = self.cfg.model_copy(deep=True)
        cfg.error_types = list(group_keys)
        if scope.model:
            cfg.api.model = scope.model
        model = cfg.api.model

        # Analyses OFF: no sweeps, no dictionary scan, no consistency/audit — a
        # targeted re-read is only the model pass over the scoped paragraphs.
        prepared = prepare(cfg, str(self.source_path), self.error_dir,
                           analyses=False)

        # Restrict the read to the scoped paragraphs by rechunking a document
        # filtered to just those ids (in reading order). Not prepare(selection=),
        # which filters by chunk id and would drag a chunk's neighbours along.
        selected = set(scope.paragraph_ids(ms))
        scoped_paras = tuple(
            p for p in prepared.doc.paragraphs if p.para_id in selected)
        notes: list[str] = []
        if not scoped_paras:
            notes.append("single_pass: no scoped paragraphs were reviewable")
            return AdapterResult(findings=[], coverage_notes=notes, cost_usd=0.0)

        scoped_doc = dataclasses.replace(prepared.doc, paragraphs=scoped_paras)
        chunks = chunk_document(scoped_doc, cfg)

        # A local meter so this call's cost is exactly its own; merged into the
        # shared usage afterwards. process_result requires the same Usage it
        # threads, so it gets the local one.
        local_usage = Usage()
        finding_ids = itertools.count(1)
        analyzers = build_analyzers(
            cfg, prepared.groups, self.provider, finding_ids,
            vocabulary=prepared.vocabulary, conventions=prepared.conventions,
            story=prepared.story_sheet)

        collected = []
        passes = max(1, scope.passes)
        for _ in range(passes):
            for analyzer in analyzers:
                for chunk in chunks:
                    # fetch is pure/network; process_result advances the shared
                    # id counter and meters tokens, so it runs serially.
                    result = analyzer.fetch(chunk)
                    found, _answered = analyzer.process_result(
                        result, chunk, local_usage)
                    collected.extend(found)

        # Anchor and channel the raw findings. prepared.doc (full) is the anchor
        # source so paragraph lookups always resolve.
        validated = validate_findings(
            collected, prepared.doc, cfg.min_confidence,
            query_types=prepared.query_types, format_types=prepared.format_types,
            edit_guard=cfg.edit_guard)

        gfindings: list[GFinding] = []
        dropped = 0
        queries = 0
        seen: set[tuple] = set()
        for f in validated:
            # A finding the validator both anchored AND kept as a tracked
            # change is a real correction. An anchored "query" is kept too, as
            # confidence="query" with no replace text — its insert_text is
            # always empty, a question not a fix — and the deliverable turns
            # it into a margin comment. A "skipped_low_confidence" or rejected
            # finding carries an Anchor as well (every channel gets one) but no
            # real fix, and would hand the case file a fabricated deletion if
            # converted. See docproof_ladder.gfindings_from_json's matching
            # guard.
            if f.anchor is None or f.status not in ("validated", "query"):
                dropped += 1
                continue
            query = f.status == "query"
            key = (f.para_id, f.anchor.start, f.anchor.end, f.error_type,
                   f.anchor.delete_text, "" if query else f.anchor.insert_text)
            if key in seen:
                # A repeated pass (scope.passes>1) re-finds the same edit; union
                # by identity so a second read adds only what it uniquely caught.
                continue
            seen.add(key)
            # Content-derived id: the orchestrator unions findings across waves
            # by id alone, and the run-local "f-NNNN" counter restarts every
            # call, so wave 3's "f-0007" would silently swallow wave 2's. A
            # hash of the edit's identity makes a genuine cross-wave re-find
            # dedupe and keeps distinct findings from ever colliding.
            digest = hashlib.sha1(
                "\x00".join(str(part) for part in key).encode("utf-8")
            ).hexdigest()[:10]
            queries += int(query)
            gfindings.append(GFinding(
                id=f"sp-{digest}",
                error_type=f.error_type,
                span=Span(f.para_id, f.anchor.start, f.anchor.end),
                find=f.anchor.delete_text,
                replace="" if query else f.anchor.insert_text,
                note=f.explanation,
                confidence="query" if query else f.confidence,
                provenance=Provenance(
                    detector=self.name, wave=self.wave, model=model,
                    cost_usd=0.0),
            ))

        if queries:
            notes.append(
                f"{queries} finding(s) carried as queries (margin comments, "
                f"not tracked changes)")
        if dropped:
            notes.append(
                f"{dropped} finding(s) had no anchor or no applied fix and were "
                f"dropped from the union")

        # Thread this call's spend onto the shared usage, then price the local
        # meter alone for a clean per-call cost.
        _merge_usage(usage, local_usage)
        cost = cost_of_usage(local_usage, fallback_model=model) or 0.0

        return AdapterResult(findings=gfindings, coverage_notes=notes,
                            cost_usd=cost)


__all__ = ["SinglePassAdapter"]
