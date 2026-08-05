"""The pipeline as three reusable steps.

`prepare` and `finish` bookend every route into docproof — the CLI's `review`,
the batch collector, and the app's job runner. Only the middle differs: one
sends chunks to a provider now, the other picks results up hours later.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .analyzer import Analyzer
from .chunker import chunk_document
from .config import Config
from .error_registry import ErrorType, load_error_types
from .formats import DocumentFormat, get_format
from .audit import AuditReport, enforce, run_audit
from .changelog import write_change_log
from .models import Chunk, DocumentModel, Usage
from .normalize import NormalizationReport
from .providers import Provider
from .reporting import write_findings_json, write_summary_md
from .spellscan import SpellScan, scan as spell_scan
from .sweeps import SweepReport, run_sweeps
from .validator import validate_findings

log = logging.getLogger("docproof.pipeline")


@dataclass
class Prepared:
    pkg: object                  # the format's package wrapper
    doc: DocumentModel
    chunks: list[Chunk]
    groups: list[list[ErrorType]]
    fmt: DocumentFormat
    # The deterministic sweeps have already run by the time a Prepared exists:
    # they need no API call, so there is nothing to defer them for, and having
    # them here means `inventory` can show what a run would fix for free.
    sweep_findings: list = field(default_factory=list)
    sweep_reports: list[SweepReport] = field(default_factory=list)
    # The dictionary scan, for the same reason: it makes no API call, and its
    # result is the same for every chunk, so it belongs to the document.
    spell: SpellScan = field(default_factory=SpellScan)
    normalization: NormalizationReport = field(
        default_factory=NormalizationReport)
    # Every paragraph's text as ingested, including the ones no pass reviews:
    # the audit has to cover a heading the normalizer touched just as much as
    # a body paragraph the model edited.
    baseline: dict = field(default_factory=dict)

    @property
    def query_types(self) -> frozenset[str]:
        """Error types that ask rather than correct, taken from the types this
        run actually loaded — so an override that changes a type's channel is
        honoured without anything else needing to know."""
        return frozenset(et.key for group in self.groups for et in group
                         if et.is_query)

    @property
    def vocabulary(self) -> str:
        """The manuscript's own vocabulary, as a system-prompt section."""
        return self.spell.prompt_section()

    @property
    def content_hash(self) -> str:
        return content_hash(self.doc)

    @property
    def request_count(self) -> int:
        return len(self.chunks) * len(self.groups)

    @property
    def est_document_tokens(self) -> int:
        return sum(c.est_tokens for c in self.chunks) * len(self.groups)


@dataclass(frozen=True)
class Outputs:
    reviewed_path: Path
    summary_md: Path
    findings_json: Path
    applied: int
    findings: int
    change_log: Path | None = None


def content_hash(doc: DocumentModel) -> str:
    """Fingerprint of the reviewable text, in walk order.

    A batch is collected in a different process — possibly days later — so the
    document is re-ingested from disk at that point. If the author edited it
    meanwhile, paragraph offsets have moved and every anchor would land in the
    wrong place. Comparing this hash turns that into a clean failure."""
    h = hashlib.sha256()
    for p in doc.paragraphs:
        h.update(p.para_id.encode("utf-8"))
        h.update(b"\0")
        h.update(p.text.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def prepare(cfg: Config, input_path: str | Path, error_dir: str | Path, *,
            max_chunks: int | None = None,
            selection: Sequence[str] | None = None) -> Prepared:
    """Ingest, chunk, and resolve error types. Raises IngestError on a document
    docproof refuses to touch (tracked changes, corruption).

    `selection` narrows the run to specific chunk ids. Chunking itself always
    runs over the whole document, so ids and paragraph offsets mean the same
    thing whether or not a subset was picked — that is what lets a batch job
    reproduce its own chunk list at collection time."""
    fmt = get_format(input_path)
    pkg = fmt.preflight(str(input_path), cfg.tracked_changes_policy)

    # The two silent edits happen here, before anything reads the text, so no
    # later stage has to know they happened: the document model, the sweeps,
    # every model pass and every anchor all measure against normalized text.
    norm = NormalizationReport()
    if fmt.normalize is not None:
        norm = fmt.normalize(pkg, quotes=cfg.normalize.quotes,
                             spaces=cfg.normalize.spaces)
    elif cfg.normalize.quotes or cfg.normalize.spaces:
        log.info("%s files are not normalized; quotes and spacing are left as "
                 "the author's application wrote them.", fmt.suffix)

    # The audit's yardstick, taken after normalization and before any tracked
    # change: this is "the document as ingested", and rejecting every revision
    # must return to exactly it.
    baseline = fmt.snapshot(pkg, "current") if fmt.snapshot else {}
    doc = fmt.build_document_model(pkg, cfg)
    chunks = list(chunk_document(doc, cfg))
    if selection is not None:
        wanted = set(selection)
        unknown = wanted - {c.chunk_id for c in chunks}
        if unknown:
            raise ValueError(
                f"No such section(s) in this document: "
                f"{', '.join(sorted(unknown))}")
        chunks = [c for c in chunks if c.chunk_id in wanted]
        log.info("Reviewing %d selected section(s)", len(chunks))
    if max_chunks:
        chunks = chunks[:max_chunks]
        log.info("Reviewing only the first %d chunk(s)", len(chunks))
    registry = load_error_types(error_dir, cfg.error_type_keys,
                                override_dir=cfg.error_type_override_dir)
    groups = [[registry[k] for k in group] for group in cfg.error_type_groups]
    log.info("%d error type(s) in %d pass(es): %s", len(cfg.error_type_keys),
             len(groups), "; ".join("+".join(g) for g in cfg.error_type_groups))

    # Sweeps read the whole paragraph from the document model, not the chunk,
    # so their offsets and occurrence counts are measured against the same
    # canonical text the validator anchors into — an oversized paragraph split
    # across chunks would otherwise have them counting within a slice. They
    # still only touch paragraphs the run actually covers: a selected-sections
    # run must not edit the rest of the manuscript.
    covered = {p.para_id for c in chunks for p in c.paragraphs}
    whole = selection is None and not max_chunks
    swept = [p for p in doc.paragraphs
             # On a whole-document run the sweeps also reach the paragraphs
             # too short for a model pass — in fiction those are lines of
             # dialogue, and a "?!" or a mispunctuated tag is exactly what
             # lives there. A partial run stays inside its selection, because
             # editing the rest of the book is not what was asked for.
             if p.para_id in covered or (whole and not p.reviewable)]
    sweep_findings, sweep_reports = run_sweeps(swept, cfg.sweeps)

    # The spell scan reads the WHOLE document even when the run covers a few
    # sections. It changes nothing, so reading more costs nothing — and a
    # coined name is only recognisable as the author's by being used across
    # the manuscript. Scanning one chapter would file its own hero's name as a
    # suspected typo.
    spell = spell_scan(doc.paragraphs, enabled=cfg.spellcheck.enabled,
                       min_occurrences=cfg.spellcheck.min_occurrences,
                       suggestion_limit=cfg.spellcheck.suggestion_limit,
                       allowlist=cfg.spellcheck.allowlist,
                       dictionary=cfg.spellcheck.dictionary)
    return Prepared(pkg=pkg, doc=doc, chunks=chunks, groups=groups, fmt=fmt,
                    sweep_findings=sweep_findings, sweep_reports=sweep_reports,
                    spell=spell, normalization=norm, baseline=baseline)


def chunk_outline(prepared: Prepared) -> list[dict]:
    """One row per chunk for a picker UI: what the section is, and what it
    would cost to review. Preview text comes from the document itself, so it
    is the user's own prose, not an id they have no way to recognise."""
    rows = []
    for chunk in prepared.chunks:
        first = chunk.paragraphs[0]
        preview = " ".join(first.text.split())
        rows.append({
            "chunk_id": chunk.chunk_id,
            "paragraphs": len(chunk.paragraphs),
            "est_tokens": chunk.est_tokens,
            "style": first.style,
            "location": first.location,
            "preview": preview[:160] + ("…" if len(preview) > 160 else ""),
        })
    return rows


def build_analyzers(cfg: Config, groups: list[list[ErrorType]],
                    provider: Provider | None,
                    finding_ids: itertools.count,
                    vocabulary: str = "") -> list[Analyzer]:
    return [Analyzer(cfg, group, provider, finding_ids, vocabulary)
            for group in groups]


def run_sync(cfg: Config, prepared: Prepared, provider: Provider, *,
             progress=None, checkpoint=None) -> tuple[list, Usage]:
    """Review every chunk now, one API call per (pass, chunk).

    `progress` is called with (done, total) after each call so a UI can show
    movement; it is the only reason this loop isn't a comprehension.

    `checkpoint` (a docproof.checkpoint.Checkpoint, already loaded) makes the
    run resumable: each completed call's findings land in it as they arrive,
    and calls it already holds are replayed instead of paid for again. The
    replay happens *in loop order* — the validator gives earlier findings
    first claim on a span, so order is part of the result, not presentation."""
    from .batch import custom_id
    from .checkpoint import (add_usage, finding_from_dict, finding_to_dict,
                             snapshot, usage_delta)

    start = (checkpoint.max_finding_id() + 1) if checkpoint else 1
    ids = itertools.count(start)
    usage = Usage()
    findings: list = []
    total = prepared.request_count
    done = 0
    for i, analyzer in enumerate(build_analyzers(cfg, prepared.groups,
                                                 provider, ids,
                                                 prepared.vocabulary)):
        for chunk in prepared.chunks:
            key = custom_id(i, chunk.chunk_id)
            cached = checkpoint.get(key) if checkpoint else None
            if cached is not None:
                findings.extend(finding_from_dict(d) for d in cached.items)
                add_usage(usage, cached.usage)
            else:
                before = snapshot(usage)
                found, ok = analyzer.analyze_chunk(chunk, usage)
                findings.extend(found)
                if checkpoint:
                    checkpoint.put(
                        key, items=[finding_to_dict(f) for f in found],
                        usage=usage_delta(before, usage), ok=ok)
            done += 1
            if progress:
                progress(done, total)
    return findings, usage


def finish(prepared: Prepared, findings: list, usage: Usage, cfg: Config, *,
           out_dir: str | Path, source_path: str | Path,
           batch: bool = False) -> Outputs:
    """Validate, write tracked changes, save, and report."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Sweep findings go first: the validator gives the earliest finding to
    # claim a span the right to it, and a scripted rule is more certain than
    # anything the model reports about the same characters.
    validated = validate_findings(list(prepared.sweep_findings) + list(findings),
                                  prepared.doc, cfg.min_confidence,
                                  query_types=prepared.query_types)
    fmt = prepared.fmt
    stats = fmt.apply_tracked_changes(prepared.pkg, prepared.doc, validated, cfg)

    audit_report = AuditReport()
    if cfg.audit != "off" and fmt.snapshot and prepared.baseline:
        audit_report = run_audit(prepared.baseline,
                                 fmt.snapshot(prepared.pkg, "reject"))

    # The reports are written before the audit is enforced, and the document
    # after it. A failed strict run therefore produces the two files that say
    # what went wrong and no manuscript at all — which is the only honest
    # meaning of refusing to ship, and still leaves a diagnosis behind.
    reviewed = out / fmt.reviewed_name(source_path)
    write_findings_json(out / "findings.json", doc=prepared.doc,
                        findings=validated, usage=usage, cfg=cfg,
                        applied_ids=stats.applied, batch=batch,
                        sweeps=prepared.sweep_reports, spell=prepared.spell,
                        normalization=prepared.normalization,
                        audit=audit_report)
    write_summary_md(out / "summary.md", doc=prepared.doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied,
                     batch=batch, fmt=fmt, sweeps=prepared.sweep_reports,
                     spell=prepared.spell,
                     normalization=prepared.normalization, audit=audit_report)
    change_log = None
    if cfg.change_log:
        change_log = out / fmt.change_log_name(source_path)
        write_change_log(change_log, doc=prepared.doc, findings=validated,
                         cfg=cfg, applied_ids=stats.applied,
                         sweeps=prepared.sweep_reports, spell=prepared.spell,
                         normalization=prepared.normalization,
                         audit=audit_report, usage=usage, stats=stats)

    enforce(audit_report, cfg.audit)
    prepared.pkg.save(reviewed)
    return Outputs(reviewed_path=reviewed, summary_md=out / "summary.md",
                   findings_json=out / "findings.json",
                   applied=len(stats.applied), findings=len(validated),
                   change_log=change_log)
