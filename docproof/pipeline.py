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
from .consistency import (CONSISTENCY_KEY, ConsistencyReport,
                          find_inconsistencies, to_findings)
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
from .variants import Variant, load_variant

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
    variant: Variant | None = None
    # Terms the manuscript writes more than one way. Document-wide, so it
    # belongs here rather than to any chunk, and it costs no API call.
    consistency: ConsistencyReport = field(default_factory=ConsistencyReport)
    consistency_findings: list = field(default_factory=list)

    @property
    def conventions(self) -> str:
        """The variant's rules, as a system-prompt section."""
        return self.variant.prompt_section() if self.variant else ""

    @property
    def query_types(self) -> frozenset[str]:
        """Error types that ask rather than correct, taken from the types this
        run actually loaded — so an override that changes a type's channel is
        honoured without anything else needing to know."""
        # The consistency scan has no error type — there is no prompt to
        # write, since it is decided before any model sees the document — but
        # its findings go down the same channel.
        return frozenset([CONSISTENCY_KEY]) | frozenset(
            et.key for group in self.groups for et in group if et.is_query)

    @property
    def format_types(self) -> dict[str, str]:
        """Error types that change how text is set, and what they set it to."""
        return {et.key: et.format for group in self.groups for et in group
                if et.is_format}

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
            selection: Sequence[str] | None = None,
            analyses: bool = True) -> Prepared:
    """Ingest, chunk, and resolve error types. Raises IngestError on a document
    docproof refuses to touch (tracked changes, corruption).

    `selection` narrows the run to specific chunk ids. Chunking itself always
    runs over the whole document, so ids and paragraph offsets mean the same
    thing whether or not a subset was picked — that is what lets a batch job
    reproduce its own chunk list at collection time.

    `analyses=False` skips the whole-document passes that make no API call but
    read every paragraph — the sweeps, the dictionary scan, the consistency
    pass, and the audit baseline. A drop-time preflight only wants section and
    token counts and throws all of that away, so paying for it there (the spell
    scan's Hunspell suggestions are seconds per manuscript) is pure latency.
    The real run leaves it on."""
    fmt = get_format(input_path)
    variant = load_variant(cfg.variant)
    log.info("Proofreading as %s (%s)", variant.name,
             "; ".join(variant.authorities) or variant.dictionary)
    pkg = fmt.preflight(str(input_path), cfg.tracked_changes_policy)

    # The two silent edits happen here, before anything reads the text, so no
    # later stage has to know they happened: the document model, the sweeps,
    # every model pass and every anchor all measure against normalized text.
    norm = NormalizationReport()
    if fmt.normalize is not None:
        norm = fmt.normalize(pkg, quotes=cfg.normalize.quotes,
                             spaces=cfg.normalize.spaces, variant=variant)
    elif cfg.normalize.quotes or cfg.normalize.spaces:
        log.info("%s files are not normalized; quotes and spacing are left as "
                 "the author's application wrote them.", fmt.suffix)

    # The audit's yardstick, taken after normalization and before any tracked
    # change: this is "the document as ingested", and rejecting every revision
    # must return to exactly it. A counts-only preflight never audits anything,
    # so it skips the snapshot.
    baseline = fmt.snapshot(pkg, "current") if (analyses and fmt.snapshot) else {}
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

    whole = selection is None and not max_chunks
    if analyses:
        # Sweeps read the whole paragraph from the document model, not the
        # chunk, so their offsets and occurrence counts are measured against
        # the same canonical text the validator anchors into — an oversized
        # paragraph split across chunks would otherwise have them counting
        # within a slice. They still only touch paragraphs the run actually
        # covers: a selected-sections run must not edit the rest of the
        # manuscript.
        covered = {p.para_id for c in chunks for p in c.paragraphs}
        swept = [p for p in doc.paragraphs
                 # On a whole-document run the sweeps also reach the paragraphs
                 # too short for a model pass — in fiction those are lines of
                 # dialogue, and a "?!" or a mispunctuated tag is exactly what
                 # lives there. A partial run stays inside its selection,
                 # because editing the rest of the book is not what was asked
                 # for.
                 if p.para_id in covered or (whole and not p.reviewable)]
        sweep_findings, sweep_reports = run_sweeps(
            swept, cfg.sweeps, variant, ellipsis_style=cfg.style.ellipsis)

        # The spell scan reads the WHOLE document even when the run covers a
        # few sections. It changes nothing, so reading more costs nothing — and
        # a coined name is only recognisable as the author's by being used
        # across the manuscript. Scanning one chapter would file its own hero's
        # name as a suspected typo.
        spell = spell_scan(doc.paragraphs, enabled=cfg.spellcheck.enabled,
                           min_occurrences=cfg.spellcheck.min_occurrences,
                           suggestion_limit=cfg.spellcheck.suggestion_limit,
                           allowlist=cfg.spellcheck.allowlist,
                           # An explicit dictionary wins; otherwise the variant
                           # picks one, which is the whole point of stating it.
                           dictionary=cfg.spellcheck.dictionary
                           or variant.dictionary)

        # Whole-document, and only on a whole-document run: "you write this
        # three ways" is not a claim a review of two chapters can make.
        consistency = find_inconsistencies(
            doc.paragraphs if whole else [],
            enabled=cfg.consistency.enabled and whole,
            min_length=cfg.consistency.min_length,
            min_dominance=cfg.consistency.min_dominance)
        consistency_findings = to_findings(consistency, doc.paragraphs)
    else:
        # Counts-only preflight: none of the discardable whole-document work.
        sweep_findings, sweep_reports = [], []
        spell = SpellScan()
        consistency = ConsistencyReport()
        consistency_findings = []

    return Prepared(pkg=pkg, doc=doc, chunks=chunks, groups=groups, fmt=fmt,
                    sweep_findings=sweep_findings, sweep_reports=sweep_reports,
                    spell=spell, normalization=norm, baseline=baseline,
                    variant=variant, consistency=consistency,
                    consistency_findings=consistency_findings)


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
                    vocabulary: str = "",
                    conventions: str = "") -> list[Analyzer]:
    return [Analyzer(cfg, group, provider, finding_ids, vocabulary, conventions)
            for group in groups]


def run_sync(cfg: Config, prepared: Prepared, provider: Provider, *,
             progress=None, checkpoint=None) -> tuple[list, Usage]:
    """Review every chunk now, one API call per (pass, chunk).

    The calls are independent, so up to `cfg.api.concurrency` of them are in
    flight at once — most of a review's wall-clock is waiting on the network,
    and this is where that time goes. Only the *fetch* is concurrent, though:
    the results are folded back in strict document order, because the validator
    gives an earlier finding first claim on a span, the id counter must hand out
    stable f-NNNN, and the token accounting and resumable checkpoint both depend
    on that order. So a big manuscript comes back far sooner, byte-for-byte the
    same as if it had run serially.

    `progress` is called with (done, total) as each call is folded in, in order.

    `checkpoint` (a docproof.checkpoint.Checkpoint, already loaded) makes the
    run resumable: each completed call's findings land in it as they arrive, in
    order, and calls it already holds are replayed instead of paid for again."""
    from concurrent.futures import ThreadPoolExecutor

    from .batch import custom_id
    from .checkpoint import (add_usage, finding_from_dict, finding_to_dict,
                             snapshot, usage_delta)

    start = (checkpoint.max_finding_id() + 1) if checkpoint else 1
    ids = itertools.count(start)
    analyzers = build_analyzers(cfg, prepared.groups, provider, ids,
                               prepared.vocabulary, prepared.conventions)
    # The work list, in the order results must be folded back in.
    work = [(analyzer, chunk, custom_id(i, chunk.chunk_id))
            for i, analyzer in enumerate(analyzers)
            for chunk in prepared.chunks]

    usage = Usage()
    findings: list = []
    total = prepared.request_count

    with ThreadPoolExecutor(max_workers=cfg.api.concurrency) as pool:
        # Fetch every uncached chunk concurrently; a cached call needs no
        # network and gets no future.
        futures = [
            None if (checkpoint and checkpoint.get(key) is not None)
            else pool.submit(analyzer.fetch, chunk)
            for analyzer, chunk, key in work]

        for done, ((analyzer, chunk, key), future) in enumerate(
                zip(work, futures), start=1):
            if future is None:                       # replay a cached call
                cached = checkpoint.get(key)
                findings.extend(finding_from_dict(d) for d in cached.items)
                add_usage(usage, cached.usage)
            else:
                before = snapshot(usage)
                # A failed earlier attempt at this call still paid for its
                # tokens. Folding them in ahead of the snapshot makes the
                # retry's entry cumulative, so the spend survives on disk and
                # in the totals however many resumes it takes.
                burned = checkpoint.burned(key) if checkpoint else None
                if burned:
                    add_usage(usage, burned)
                # .result() blocks only until THIS call lands; later ones keep
                # fetching in the pool meanwhile. Bookkeeping stays in order.
                found, ok = analyzer.process_result(future.result(), chunk,
                                                    usage)
                findings.extend(found)
                if checkpoint:
                    checkpoint.put(
                        key, items=[finding_to_dict(f) for f in found],
                        usage=usage_delta(before, usage), ok=ok)
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
    validated = validate_findings(list(prepared.sweep_findings)
                                  + list(prepared.consistency_findings)
                                  + list(findings),
                                  prepared.doc, cfg.min_confidence,
                                  query_types=prepared.query_types,
                                  format_types=prepared.format_types)
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
                        audit=audit_report, consistency=prepared.consistency)
    write_summary_md(out / "summary.md", doc=prepared.doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied,
                     batch=batch, fmt=fmt, sweeps=prepared.sweep_reports,
                     spell=prepared.spell,
                     normalization=prepared.normalization, audit=audit_report,
                     consistency=prepared.consistency)
    change_log = None
    if cfg.change_log:
        change_log = out / fmt.change_log_name(source_path)
        write_change_log(change_log, doc=prepared.doc, findings=validated,
                         cfg=cfg, applied_ids=stats.applied,
                         sweeps=prepared.sweep_reports, spell=prepared.spell,
                         normalization=prepared.normalization,
                         audit=audit_report, usage=usage, stats=stats,
                         variant=prepared.variant)

    enforce(audit_report, cfg.audit)
    prepared.pkg.save(reviewed)
    return Outputs(reviewed_path=reviewed, summary_md=out / "summary.md",
                   findings_json=out / "findings.json",
                   applied=len(stats.applied), findings=len(validated),
                   change_log=change_log)
