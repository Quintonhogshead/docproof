"""The pipeline as three reusable steps.

`prepare` and `finish` bookend every route into docproof — the CLI's `review`,
the batch collector, and the app's job runner. Only the middle differs: one
sends chunks to a provider now, the other picks results up hours later.
"""
from __future__ import annotations

import dataclasses
import hashlib
import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .analyzer import Analyzer
from .chunker import _SENTENCE_SPLIT, chunk_document
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


class JobCancelled(Exception):
    """Raised out of a run the moment the user aborts it, so the caller can
    mark the job cancelled rather than failed. Not an error: nothing went
    wrong, the work was called off."""


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
    # Suspected real-word typos to put to the model, one per occurrence. Whole-
    # document only (it reads the spell scan's lexicon), and consumed on the
    # synchronous path; empty otherwise. See docproof/adjudicate.py.
    adjudicate_candidates: list = field(default_factory=list)
    # How many detectors review each chunk. 1 unless the ensemble is on, and the
    # multiplier the request count and token estimate need — every detector is
    # the whole review over again.
    n_detectors: int = 1

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
        return len(self.chunks) * len(self.groups) * self.n_detectors

    @property
    def est_document_tokens(self) -> int:
        # Context paragraphs ride the user turn and are billed on every chunk of
        # every pass, so they belong in the tokens-sent estimate even though
        # they are not the document's own text. Every detector sends the whole
        # thing again, hence the n_detectors multiplier.
        from .utils.tokens import estimate_tokens
        own = sum(c.est_tokens for c in self.chunks)
        context = sum(estimate_tokens(p.text)
                      for c in self.chunks for p in c.context_paragraphs)
        return (own + context) * len(self.groups) * self.n_detectors


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
    all_chunk_ids = {c.chunk_id for c in chunks}
    if selection is not None:
        wanted = set(selection)
        unknown = wanted - all_chunk_ids
        if unknown:
            raise ValueError(
                f"No such section(s) in this document: "
                f"{', '.join(sorted(unknown))}")
        chunks = [c for c in chunks if c.chunk_id in wanted]
        if len(chunks) < len(all_chunk_ids):
            log.info("Reviewing %d selected section(s)", len(chunks))
    if max_chunks:
        chunks = chunks[:max_chunks]
        log.info("Reviewing only the first %d chunk(s)", len(chunks))
    registry = load_error_types(error_dir, cfg.error_type_keys,
                                override_dir=cfg.error_type_override_dir)
    groups = [[registry[k] for k in group] for group in cfg.error_type_groups]
    log.info("%d error type(s) in %d pass(es): %s", len(cfg.error_type_keys),
             len(groups), "; ".join("+".join(g) for g in cfg.error_type_groups))

    # A selection that names every chunk IS a whole-document run. The batch
    # manifest deliberately records the resolved chunk ids of a full review
    # (reproducibility), and collect re-prepares from that manifest — treating
    # the explicit-but-complete list as a partial run silently dropped the
    # whole-document analyses (the consistency scan, sweeps on paragraphs no
    # chunk covers) from every collected batch job.
    whole = not max_chunks and (selection is None
                                or set(selection) >= all_chunk_ids)
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
            min_dominance=cfg.consistency.min_dominance,
            names=cfg.consistency.names,
            name_dominance=cfg.consistency.name_dominance,
            name_min_count=cfg.consistency.name_min_count)
        consistency_findings = to_findings(consistency, doc.paragraphs)

        # Suspected typos to adjudicate later, generated now off the same spell
        # scan. Whole-document only (it reads the lexicon) and consumed on the
        # synchronous path; producing it here keeps every deterministic,
        # document-wide signal in one place.
        adjudicate_candidates = []
        if cfg.adjudicate.enabled and whole:
            from .adjudicate import generate
            adjudicate_candidates = generate(
                doc.paragraphs, protected=spell.lexicon,
                dictionary=cfg.spellcheck.dictionary or variant.dictionary,
                near_miss_gap=cfg.adjudicate.near_miss_gap,
                min_len=cfg.adjudicate.min_word_len,
                max_candidates=cfg.adjudicate.max_candidates)
    else:
        # Counts-only preflight: none of the discardable whole-document work.
        sweep_findings, sweep_reports = [], []
        spell = SpellScan()
        consistency = ConsistencyReport()
        consistency_findings = []
        adjudicate_candidates = []

    return Prepared(pkg=pkg, doc=doc, chunks=chunks, groups=groups, fmt=fmt,
                    sweep_findings=sweep_findings, sweep_reports=sweep_reports,
                    spell=spell, normalization=norm, baseline=baseline,
                    variant=variant, consistency=consistency,
                    consistency_findings=consistency_findings,
                    adjudicate_candidates=adjudicate_candidates,
                    n_detectors=len(cfg.ensemble.detectors) or 1)


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


def _subchunk(chunk: Chunk, paragraphs, suffix: str) -> Chunk:
    from .utils.tokens import estimate_tokens
    return Chunk(f"{chunk.chunk_id}-{suffix}", tuple(paragraphs),
                 sum(estimate_tokens(p.text) for p in paragraphs))


def _split_for_retry(chunk: Chunk) -> list[Chunk]:
    """Two smaller chunks to re-ask after a truncation. Prefer halving the
    paragraph list; for a lone paragraph that truncated on its own, halve its
    sentences — the same last resort the chunker uses for an oversized
    paragraph, and it inherits the same occurrence caveat. Empty means there is
    nothing left to split, and the caller gives up rather than loop."""
    paras = chunk.paragraphs
    if len(paras) > 1:
        mid = len(paras) // 2
        return [_subchunk(chunk, paras[:mid], "a"),
                _subchunk(chunk, paras[mid:], "b")]
    sents = [s for s in _SENTENCE_SPLIT.split(paras[0].text) if s]
    if len(sents) < 2:
        return []
    mid = len(sents) // 2
    left = dataclasses.replace(paras[0], text=" ".join(sents[:mid]))
    right = dataclasses.replace(paras[0], text=" ".join(sents[mid:]))
    return [_subchunk(chunk, (left,), "a"), _subchunk(chunk, (right,), "b")]


def _retry_failed(analyzer: Analyzer, chunk: Chunk, result, usage: Usage
                  ) -> tuple[list, bool]:
    """One bounded semantic retry for a (pass, chunk) the provider did not
    answer, so a single hiccup no longer drops a whole section silently.

    Only the two failures a fresh attempt *in this run* can fix are acted on: a
    truncation — where re-sending the same request would truncate at the same
    place, so the chunk is split and each half asked again — and a refusal,
    which is stochastic and re-asked once. A transport error has already
    outlived the SDK's own retries, and a schema-invalid reply is usually a
    persistent quirk of the request; re-asking either immediately tends to buy
    the same failure at twice the price, so both are left for the resumable
    checkpoint to re-call on the next run and are recorded as a coverage gap in
    the meantime. Bounded to one level so a failing chunk cannot loop; every
    attempt's tokens are counted into `usage`."""
    if result.stop_reason == "max_tokens":
        parts = _split_for_retry(chunk)
        if not parts:
            return [], False
        out: list = []
        ok_any = False
        for part in parts:
            found, ok = analyzer.process_result(analyzer.fetch(part), part, usage)
            out.extend(found)
            ok_any = ok_any or ok
        if ok_any:
            log.info("%s [%s]: recovered by splitting the chunk after a "
                     "truncation", chunk.chunk_id, analyzer.label)
        return out, ok_any
    if result.stop_reason == "refusal":
        found, ok = analyzer.process_result(analyzer.fetch(chunk), chunk, usage)
        if ok:
            log.info("%s [%s]: recovered on a second attempt", chunk.chunk_id,
                     analyzer.label)
        return found, ok
    return [], False


def analyze_with_retry(analyzer: Analyzer, chunk: Chunk, usage: Usage
                       ) -> tuple[list, bool]:
    """A fresh synchronous fetch for one (pass, chunk), with the same one-shot
    retry as the concurrent path. Used to recover a batch request the provider
    never returned, without duplicating the retry policy."""
    result = analyzer.fetch(chunk)
    found, ok = analyzer.process_result(result, chunk, usage)
    if ok:
        return found, ok
    return _retry_failed(analyzer, chunk, result, usage)


def _detector_specs(cfg: Config) -> list[tuple[str, str | None]]:
    """(model, effort) per detector to run. Single-detector mode — the default —
    yields exactly api.model/api.effort, so the fan-out collapses to today's one
    call per (pass, chunk) and nothing downstream can tell the difference."""
    if not cfg.ensemble.enabled:
        return [(cfg.api.model, cfg.api.effort)]
    return [(d.model, d.effort) for d in cfg.ensemble.detectors]


def _ckpt_key(pass_index: int, chunk_id: str, detector: int) -> str:
    """Checkpoint key for one (pass, chunk, detector). Detector 0 keeps the
    legacy two-part key so a checkpoint written before the ensemble existed —
    or by a single-detector run — still resumes; later detectors get a suffix."""
    from .batch import custom_id
    base = custom_id(pass_index, chunk_id)
    return base if detector == 0 else f"{base}-d{detector}"


def run_sync(cfg: Config, prepared: Prepared, provider: Provider | None = None,
             *, progress=None, checkpoint=None, should_cancel=None,
             coverage=None, provider_factory=None) -> tuple[list, Usage]:
    """Review every chunk now, one API call per (pass, chunk, detector).

    In single-detector mode (the default) that is one call per (pass, chunk),
    exactly as before. When the ensemble is on, every detector reviews every
    chunk; the calls are still independent, so up to `cfg.api.concurrency` are in
    flight at once, and results are folded back in strict (pass, chunk, detector)
    order — the order the id counter, the token accounting and the resumable
    checkpoint all depend on. So a big manuscript comes back far sooner,
    byte-for-byte the same as if it had run serially.

    `provider` is the single detector's client in legacy mode; when the ensemble
    is on it is ignored and `provider_factory` (default `build_provider`) builds
    one client per detector model up front, so a missing key fails before any
    call rather than mid-run.

    `progress` is called with (done, total) as each call is folded in, in order.

    `checkpoint` (a docproof.checkpoint.Checkpoint, already loaded) makes the
    run resumable: each completed call's findings land in it as they arrive, in
    order, and calls it already holds are replayed instead of paid for again.

    `should_cancel`, if given, is polled once per folded call; when it first
    returns true the run raises `JobCancelled` and cancels every call not yet
    started, so an abort stops paying for the tail of the document. Calls
    already in flight (at most `cfg.api.concurrency`) still finish — a thread
    pool can't recall them — but their results are dropped."""
    from concurrent.futures import ThreadPoolExecutor

    from .checkpoint import (add_usage, finding_from_dict, finding_to_dict,
                             snapshot, usage_delta)

    if provider_factory is None:
        from .providers import build_provider
        provider_factory = build_provider

    start = (checkpoint.max_finding_id() + 1) if checkpoint else 1
    ids = itertools.count(start)

    # One set of per-pass analyzers per detector. In legacy mode the single
    # detector reuses the caller's cfg and provider unchanged; in ensemble mode
    # each detector gets a cfg copy with its own model/effort and its own client
    # (built now, so a missing key raises before a single token is spent).
    specs = _detector_specs(cfg)
    ensemble = cfg.ensemble.enabled
    det_analyzers = []
    for model, effort in specs:
        if not ensemble:
            dcfg = cfg
            dprov = provider if provider is not None else provider_factory(cfg)
        else:
            dcfg = cfg.model_copy(deep=True)
            dcfg.api.model = model
            dcfg.api.effort = effort
            dprov = provider_factory(dcfg)
        det_analyzers.append(build_analyzers(
            dcfg, prepared.groups, dprov, ids,
            prepared.vocabulary, prepared.conventions))

    # The work list, in the order results must be folded back in: pass, then
    # chunk, then detector. With one detector this is (pass, chunk), the legacy
    # order and legacy keys.
    work = []
    for pass_i in range(len(prepared.groups)):
        for chunk in prepared.chunks:
            for d in range(len(specs)):
                work.append((det_analyzers[d][pass_i], chunk,
                             _ckpt_key(pass_i, chunk.chunk_id, d), d, pass_i))

    usage = Usage()
    findings: list = []
    total = prepared.request_count
    # Coverage is per (pass, chunk), not per call: a section is reviewed if ANY
    # detector answered for it, and a gap only when every detector failed it.
    covered: dict = {}

    with ThreadPoolExecutor(max_workers=cfg.api.concurrency) as pool:
        # Fetch every uncached chunk concurrently; a cached call needs no
        # network and gets no future.
        futures = [
            None if (checkpoint and checkpoint.get(key) is not None)
            else pool.submit(analyzer.fetch, chunk)
            for analyzer, chunk, key, _, _ in work]

        for done, ((analyzer, chunk, key, d, pass_i), future) in enumerate(
                zip(work, futures), start=1):
            if should_cancel and should_cancel():
                # Drop every call not already running so the abort stops the
                # spend, not just the folding. The with-block's shutdown waits
                # out the few still in flight; we raise past it.
                for pending in futures:
                    if pending is not None:
                        pending.cancel()
                raise JobCancelled()
            if future is None:                       # replay a cached call
                cached = checkpoint.get(key)
                findings.extend(finding_from_dict(x) for x in cached.items)
                add_usage(usage, cached.usage)
                ok = True                            # only ok calls are cached
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
                result = future.result()
                found, ok = analyzer.process_result(result, chunk, usage)
                # A provider non-answer used to drop the whole (pass, chunk)
                # here with only a log line. One bounded retry recovers most of
                # them; whatever it can't is recorded below as a coverage gap.
                if not ok:
                    found, ok = _retry_failed(analyzer, chunk, result, usage)
                if ensemble:
                    found = [dataclasses.replace(f, detector=d) for f in found]
                findings.extend(found)
                if checkpoint:
                    checkpoint.put(
                        key, items=[finding_to_dict(f) for f in found],
                        usage=usage_delta(before, usage), ok=ok)
            if coverage is not None:
                pc = (pass_i, chunk.chunk_id)
                prev = covered.get(pc)
                covered[pc] = (analyzer.label, chunk,
                               (prev[2] if prev else False) or ok)
            if progress:
                progress(done, total)

    if coverage is not None:
        for label, chunk, ok in covered.values():
            coverage.record(label, chunk, ok)

    # The adjudication pass: put the deterministically-found typo candidates to
    # the model and fold its confident corrections in with the rest. Runs after
    # the detector loop (it is not per-chunk), on the base model — so a resume
    # whose detector calls all replay from the checkpoint re-runs only this,
    # cheaply. Sync path only; the batch path does not carry it yet.
    if cfg.adjudicate.enabled and prepared.adjudicate_candidates:
        from .adjudicate import adjudicate
        adj_provider = provider if provider is not None else provider_factory(cfg)
        findings.extend(adjudicate(
            prepared.adjudicate_candidates, prepared.doc.paragraphs,
            adj_provider, model=cfg.api.model,
            max_tokens=cfg.api.max_output_tokens, usage=usage, ids=ids,
            batch_size=cfg.adjudicate.batch_size,
            edit_confidence=cfg.adjudicate.edit_confidence))
    return findings, usage


def finish(prepared: Prepared, findings: list, usage: Usage, cfg: Config, *,
           out_dir: str | Path, source_path: str | Path,
           batch: bool = False, coverage=None, verify_provider=None) -> Outputs:
    """Validate, write tracked changes, save, and report.

    `coverage` (a CoverageLedger, if the caller tracked one) records which
    (pass, chunk) units were actually reviewed, so the report can name any that
    the model never answered rather than letting the gap pass silently.

    `verify_provider` is the overseer-verifier's client; when the ensemble
    configures a verifier and none is passed, one is built from
    ensemble.verifier_model. Ignored entirely in single-detector mode."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # With the ensemble on, fold the detectors' findings into one list first,
    # counting agreement, so the validator downstream sees a single already-
    # merged set rather than N near-duplicates competing for the same span.
    # Sweeps and consistency are deterministic single sources and skip the merge.
    model_findings = list(findings)
    verifier_rejected: list = []
    if cfg.ensemble.enabled:
        from .agreement import merge
        model_findings = merge(model_findings, prepared.doc)
        ens = cfg.ensemble
        if ens.verifier_model and ens.verify_policy != "none":
            from .verifier import verify_findings
            if verify_provider is None:
                from .providers import build_provider
                vcfg = cfg.model_copy(deep=True)
                vcfg.api.model = ens.verifier_model
                vcfg.api.effort = ens.verifier_effort
                verify_provider = build_provider(vcfg)
            model_findings, verifier_rejected = verify_findings(
                cfg, prepared, model_findings, verify_provider, usage)
    # Sweep findings go first: the validator gives the earliest finding to
    # claim a span the right to it, and a scripted rule is more certain than
    # anything the model reports about the same characters.
    validated = validate_findings(list(prepared.sweep_findings)
                                  + list(prepared.consistency_findings)
                                  + model_findings,
                                  prepared.doc, cfg.min_confidence,
                                  query_types=prepared.query_types,
                                  format_types=prepared.format_types,
                                  edit_guard=cfg.edit_guard)
    # Verifier rejections were never candidates for a tracked change, but they
    # belong in the report so the author sees what the overseer set aside.
    validated = validated + verifier_rejected
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
                        audit=audit_report, consistency=prepared.consistency,
                        coverage=coverage)
    write_summary_md(out / "summary.md", doc=prepared.doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied,
                     batch=batch, fmt=fmt, sweeps=prepared.sweep_reports,
                     spell=prepared.spell,
                     normalization=prepared.normalization, audit=audit_report,
                     consistency=prepared.consistency, coverage=coverage)
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
