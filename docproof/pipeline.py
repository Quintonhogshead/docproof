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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

from .analyzer import Analyzer
from .chunker import _SENTENCE_SPLIT, chunk_document
from .config import Config, cache_dir_for
from .consistency import (CONSISTENCY_KEY, ConsistencyReport,
                          find_inconsistencies, to_findings)
from .error_registry import ErrorType, load_error_types
from .formats import DocumentFormat, get_format
from .audit import AuditReport, enforce, run_audit
from .changelog import write_change_log
from .models import Chunk, DocumentModel, Finding, Usage
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


@dataclass(frozen=True)
class PassRun:
    """One expanded detector pass, ready to run. `index` is the pass_index in
    every (pass, chunk) key — checkpoint keys, batch custom ids, coverage — so
    it must be unique and stable. `chunks` is the set this pass reads: a category
    with its own `token_budget` carries its own chunking here, while every
    default-budget pass shares the one set. A category read `repeats` times
    contributes that many PassRuns with identical types and chunks but distinct
    indices, so their keys never collide and the validator dedups the overlap."""
    index: int
    types: tuple[ErrorType, ...]
    chunks: tuple[Chunk, ...]
    token_budget: int
    repeat: int = 1              # 1-based: which read of the category this is
    repeats: int = 1             # how many reads the category asks for


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
    # The whole-book story sheet as a ready-to-inject system-prompt section
    # (narrator, tense, character pronouns), or "" when the pass is off. Built at
    # prepare time because it feeds the detector prompts. See docproof/storysheet.py.
    story_sheet: str = ""
    # Whether this run covers the whole manuscript. The document-wide model work
    # (the glossary pass) only means something on a full run, the same way the
    # consistency scan does; a two-chapter review must not spend on it.
    whole_document: bool = True
    # How many detectors review each chunk. 1 unless the ensemble is on, and the
    # multiplier the request count and token estimate need — every detector is
    # the whole review over again.
    n_detectors: int = 1
    # The expanded run plan: one PassRun per (category x repeat), each carrying
    # the chunk set it reads (a category with its own token_budget has its own
    # chunking). prepare() always fills this; a manually built Prepared leaves it
    # empty and `effective_pass_plan` synthesizes the historical one-pass-per-
    # group-over-`chunks` plan, so old call sites keep working unchanged.
    pass_plan: list["PassRun"] = field(default_factory=list)

    @property
    def effective_pass_plan(self) -> tuple["PassRun", ...]:
        """The run plan, synthesizing the legacy one (one pass per defined group
        over the shared `chunks`, default budget) when `pass_plan` is unset."""
        if self.pass_plan:
            return tuple(self.pass_plan)
        # Budget 0 is a "not recorded" sentinel: a hand-built Prepared has no cfg
        # to resolve it, and this synthesized plan is only ever used for its
        # types and its shared chunk set, never its budget.
        return tuple(
            PassRun(i, tuple(group), tuple(self.chunks), 0, 1, 1)
            for i, group in enumerate(self.groups))

    @property
    def pass_types(self) -> list[tuple[ErrorType, ...]]:
        """Types per pass, in pass-index order — what build_analyzers consumes so
        `analyzers[pass_index]` lines up with the plan (repeats included)."""
        return [p.types for p in self.effective_pass_plan]

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
        # One request per (pass, chunk, detector). Passes no longer share a
        # single chunk set — a repeated category contributes its chunks once per
        # read, a custom-budget category its own chunk count — so sum the plan.
        chunks = sum(len(p.chunks) for p in self.effective_pass_plan)
        return chunks * self.n_detectors

    @property
    def est_document_tokens(self) -> int:
        # Context paragraphs ride the user turn and are billed on every chunk of
        # every pass, so they belong in the tokens-sent estimate even though
        # they are not the document's own text. Every detector sends the whole
        # thing again, hence the n_detectors multiplier. Summed per pass so a
        # tighter-budget or repeated category is priced for what it actually
        # sends, not the default chunking.
        from .utils.tokens import estimate_tokens
        total = 0
        for p in self.effective_pass_plan:
            own = sum(c.est_tokens for c in p.chunks)
            context = sum(estimate_tokens(par.text)
                          for c in p.chunks for par in c.context_paragraphs)
            total += own + context
        return total * self.n_detectors


@dataclass(frozen=True)
class Outputs:
    reviewed_path: Path
    summary_md: Path
    findings_json: Path
    applied: int
    findings: int
    change_log: Path | None = None
    # The deliverable has two channels, and only one of them was ever counted
    # here. `queried` is the margin comments — questions and judgment calls the
    # author is asked to rule on, not corrections — counted exactly the way
    # summary.md counts them (status == "query" in the final list), so the two
    # never disagree. `judge_held` is how many of those queries a judge gate
    # withdrew from the tracked changes. Both default to 0 so a caller that
    # predates them still constructs.
    queried: int = 0
    judge_held: int = 0
    # Human-readable, non-fatal warnings the run wants on the job card: chiefly
    # whole passes that fell open and produced nothing (a dead or unkeyed
    # judge/continuity/glossary model). Empty on a clean run. Kept short — the
    # full accounting is summary.md's Coverage section — because the card only
    # needs to say "this 'done' run quietly skipped a paid pass, go look".
    warnings: list[str] = field(default_factory=list)


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


def _collapse_repeated_comments(validated: list, doc: DocumentModel,
                                threshold: int) -> int:
    """Leave one comment where the same rule explanation would repeat past
    `threshold`, and silence the copies (DP-008).

    2,643 comments against the humans' 31 is a review burden of its own, and
    dozens were the identical house-style sentence — 79 copies of the dash
    rule. The edits all stand; the first instance (document order) keeps the
    note, says how many siblings it speaks for, and points at the change log,
    which lists every one. Queries are never collapsed: each is its own
    question. Returns how many comments were silenced."""
    if threshold <= 0:
        return 0
    order = {p.para_id: i for i, p in enumerate(doc.paragraphs)}
    groups: dict[tuple[str, str], list[int]] = {}
    for i, f in enumerate(validated):
        if f.status != "validated" or f.silent or not f.explanation:
            continue
        groups.setdefault((f.error_type, f.explanation), []).append(i)
    silenced = 0
    for (_etype, expl), idxs in groups.items():
        if len(idxs) <= threshold:
            continue
        idxs.sort(key=lambda i: (
            order.get(validated[i].para_id, len(order)),
            validated[i].anchor.start if validated[i].anchor else 0))
        first = idxs[0]
        validated[first] = replace(validated[first], explanation=(
            f"{expl} Applied {len(idxs)} times in this manuscript; the "
            f"comment appears once here, and the change log lists every "
            f"instance."))
        for i in idxs[1:]:
            validated[i] = replace(validated[i], silent=True)
        silenced += len(idxs) - 1
    if silenced:
        log.info("Comment collapse: %d repeated rule note(s) silenced; each "
                 "rule keeps one counted comment.", silenced)
    return silenced


def _name_pair_queries(spell: SpellScan, paragraphs) -> list[Finding]:
    """One margin question per near-identical protected-name pair the spell
    scan could not call — counts too close for the lopsided-majority rule.

    A question, never a candidate: with the counts close, a model asked
    sentence by sentence has no more evidence than the counts do, and the cost
    of a wrong "fix" is a renamed character. The author knows whether these
    are one person or two; nobody else can."""
    if not spell.name_pairs:
        return []
    import re

    from .sweeps import sentence_window
    findings: list[Finding] = []
    for i, pair in enumerate(spell.name_pairs, start=1):
        pat = re.compile(r"(?<![A-Za-z’'])" + re.escape(pair.b)
                         + r"(?![A-Za-z’'])", re.IGNORECASE)
        site = next(((p, m) for p in paragraphs
                     for m in [pat.search(p.text)] if m), None)
        if site is None:
            continue
        p, m = site
        window, _lo, occ = sentence_window(p.text, m.start(), m.end())
        findings.append(Finding(
            finding_id=f"nd-{i:04d}",
            chunk_id="consistency",
            para_id=p.para_id,
            error_type="near_duplicate_name",
            original_text=window,
            occurrence=occ,
            corrected_text=window,
            explanation=(
                f"Two near-identical name spellings run through this "
                f"manuscript: “{pair.a}” ({pair.a_count}×) and “{pair.b}” "
                f"({pair.b_count}×). If they are one character, one spelling "
                f"is wrong throughout; if they are two, nothing needs to "
                f"change. The counts are too close for this pass to decide, "
                f"so both were left as written — worth settling once."),
            confidence="medium",
            force_query=True))
    return findings


def _unprotect_near_miss(spell: SpellScan, candidates) -> SpellScan:
    """Drop from the protected lexicon every word that generation escalated to a
    near-miss candidate.

    A near-miss is a word the spell scan PROTECTED as the author's own, but which
    sits one edit from a common word by a wide margin — the model is now being
    asked to rule on whether it is a repeated typo. Left in the lexicon, that same
    word would also appear in the vocabulary prompt's "words this author owns,
    never report" list, so every judge that reads it (the adjudicator, the
    multi-round Opus judge) is told to keep the word and to question it at once.
    That contradiction biases the verdict toward keeping, and is what let a
    protected misspelling survive its own correction. A word under review is no
    longer taken on trust, so it leaves the lexicon — and, with it, the
    never-report list, the change log's "author's own" count, and the
    top-of-document excluded-words note, all of which then read honestly."""
    near = {c.word.lower() for c in candidates
            if getattr(c, "kind", "") == "near_miss"}
    if not near:
        return spell
    counts = dict(zip(spell.lexicon, spell.lexicon_counts))
    kept = tuple(w for w in spell.lexicon if w.lower() not in near)
    return dataclasses.replace(
        spell, lexicon=kept,
        # The counts ride alongside the lexicon by position, so they are
        # filtered with it — a checklist that numbered the wrong words would
        # be worse than the truncated note it replaced.
        lexicon_counts=tuple(counts[w] for w in kept) if counts else ())


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

    # A chunk set per distinct token budget the categories ask for. The default
    # budget keeps the empty id prefix, so its chunk ids — and every cache,
    # checkpoint and batch id built from them — are byte-identical to a run
    # before per-category budgets existed. Only a category that overrides the
    # budget gets its own (namespaced) set, built once and shared by every
    # category on that budget.
    default_budget = cfg.chunking.token_budget
    chunk_sets: dict[int, tuple[Chunk, ...]] = {}

    def chunks_for(budget: int) -> tuple[Chunk, ...]:
        if budget not in chunk_sets:
            prefix = "" if budget == default_budget else f"b{budget}-"
            chunk_sets[budget] = chunk_document(
                doc, cfg, token_budget=budget, id_prefix=prefix)
        return chunk_sets[budget]

    chunks_for(default_budget)                       # the shared/default set
    for spec in cfg.error_type_specs:
        chunks_for(spec.token_budget or default_budget)

    # Selection and max_chunks are validated against — and applied to — every
    # budget's ids at once. Ids are namespaced by budget, so a picked id maps to
    # exactly one set and the batch manifest can pin the union unambiguously.
    all_chunk_ids = {c.chunk_id for cs in chunk_sets.values() for c in cs}
    wanted: set | None = None
    if selection is not None:
        wanted = set(selection)
        unknown = wanted - all_chunk_ids
        if unknown:
            raise ValueError(
                f"No such section(s) in this document: "
                f"{', '.join(sorted(unknown))}")

    def _select(cs: tuple[Chunk, ...]) -> tuple[Chunk, ...]:
        out = cs
        if wanted is not None:
            out = tuple(c for c in out if c.chunk_id in wanted)
        if max_chunks:
            out = out[:max_chunks]
        return out

    filtered = {b: _select(cs) for b, cs in chunk_sets.items()}
    chunks = list(filtered[default_budget])          # the set sweeps/outline read
    if wanted is not None and len(chunks) < len(chunk_sets[default_budget]):
        log.info("Reviewing %d selected section(s)", len(chunks))
    if max_chunks:
        log.info("Reviewing only the first %d chunk(s) per pass", max_chunks)

    registry = load_error_types(error_dir, cfg.error_type_keys,
                                override_dir=cfg.error_type_override_dir)
    groups = [[registry[k] for k in group] for group in cfg.error_type_groups]

    # Expand the categories into the run plan: one PassRun per (category x read),
    # each pointing at its budget's filtered chunk set, numbered so every
    # (pass, chunk) key stays unique across repeats and budgets.
    pass_plan: list[PassRun] = []
    for spec in cfg.error_type_specs:
        budget = spec.token_budget or default_budget
        types = tuple(registry[k] for k in spec.keys)
        pchunks = filtered[budget]
        for r in range(spec.passes):
            pass_plan.append(PassRun(len(pass_plan), types, pchunks,
                                     budget, r + 1, spec.passes))
    log.info("%d error type(s) in %d category/-ies, %d pass(es): %s",
             len(cfg.error_type_keys), len(groups), len(pass_plan),
             "; ".join(
                 "+".join(s.keys)
                 + (f" x{s.passes}" if s.passes > 1 else "")
                 + (f" @{s.token_budget}t" if s.token_budget else "")
                 for s in cfg.error_type_specs))

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
        # Unbalanced quotation marks ride with the sweeps but are not one: the
        # only legitimate imbalance (speech running on) needs the NEXT
        # paragraph to recognise, so it runs over the covered list whole.
        # Queries only — they claim no spans and edit nothing.
        if cfg.style.unclosed_quote_queries:
            from .sweeps import unclosed_quote_findings
            sweep_findings += unclosed_quote_findings(swept, variant)
        # The heading pass: title-case the paragraphs the skip config marks as
        # headings. Style-aware, so it cannot be a plain per-paragraph sweep,
        # but its findings and its flagged/remaining counts ride the same
        # channel — the change log quotes it beside the other scripted checks.
        if cfg.style.heading_title_case:
            from .sweeps import heading_case_findings
            heading_findings, heading_report = heading_case_findings(
                swept, cfg.skip)
            sweep_findings += heading_findings
            sweep_reports.append(heading_report)

        # The spell scan reads the WHOLE document even when the run covers a
        # few sections. It changes nothing, so reading more costs nothing — and
        # a coined name is only recognisable as the author's by being used
        # across the manuscript. Scanning one chapter would file its own hero's
        # name as a suspected typo.
        spell = spell_scan(doc.paragraphs, enabled=cfg.spellcheck.enabled,
                           min_occurrences=cfg.spellcheck.min_occurrences,
                           suggestion_limit=cfg.spellcheck.suggestion_limit,
                           allowlist=cfg.spellcheck.allowlist,
                           # The variant's respellings ride the denylist here:
                           # for the scan both are just "carries its own fix,
                           # never protected". The adjudicator gets them as
                           # their own kind, where the distinction matters.
                           denylist={**variant.respell_map,
                                     **cfg.spellcheck.denylist},
                           # An explicit dictionary wins; otherwise the variant
                           # picks one, which is the whole point of stating it.
                           dictionary=cfg.spellcheck.dictionary
                           or variant.dictionary,
                           near_duplicates=cfg.spellcheck.near_duplicates,
                           near_duplicate_dominance=(
                               cfg.spellcheck.near_duplicate_dominance))

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
        # Near-identical protected names too close to call ride the same
        # deterministic prefix as the consistency queries: one question per
        # pair, at the rarer spelling's first occurrence, changing nothing.
        consistency_findings += _name_pair_queries(spell, doc.paragraphs)

        # Suspected typos to adjudicate later, generated now off the same spell
        # scan. Whole-document only (it reads the lexicon) and consumed on the
        # synchronous path; producing it here keeps every deterministic,
        # document-wide signal in one place.
        adjudicate_candidates = []
        if cfg.adjudicate.enabled and whole:
            from .adjudicate import generate, site_word_candidates
            # The demoted near-duplicates go first: on any site both claim,
            # the dominant-name suggestion outranks a dictionary guess.
            adjudicate_candidates = site_word_candidates(
                {nd.word: nd.suggestion for nd in spell.near_duplicates},
                doc.paragraphs, kind="near_dup")
            adjudicate_candidates += generate(
                doc.paragraphs, protected=spell.lexicon,
                denylist=cfg.spellcheck.denylist,
                respell=variant.respell_map,
                dictionary=cfg.spellcheck.dictionary or variant.dictionary,
                near_miss_gap=cfg.adjudicate.near_miss_gap,
                min_len=cfg.adjudicate.min_word_len,
                max_candidates=cfg.adjudicate.max_candidates)
            # A word we are now asking about is no longer one we protect: take
            # every near-miss candidate out of the lexicon, so no judge is told
            # to keep the word and to question it in the same breath.
            spell = _unprotect_near_miss(spell, adjudicate_candidates)

        # The story sheet is the one whole-book read that happens HERE, at
        # prepare time, not at collect: it feeds the detector system prompts,
        # which are built (and, for a batch, sent) now. One cacheable call.
        story_sheet = ""
        if cfg.storysheet.enabled and whole:
            from .providers import build_provider
            from .storysheet import build_storysheet, prompt_section
            scfg = cfg.model_copy(deep=True)
            scfg.api.model = cfg.storysheet.model
            scfg.api.effort = cfg.storysheet.effort
            usage = Usage()
            sheet = build_storysheet(
                doc.paragraphs, build_provider(scfg),
                model=cfg.storysheet.model,
                max_tokens=cfg.storysheet.max_output_tokens, usage=usage,
                effort=cfg.storysheet.effort,
                cache_dir=cache_dir_for(cfg.storysheet.cache_dir))
            story_sheet = prompt_section(sheet)
    else:
        # Counts-only preflight: none of the discardable whole-document work.
        sweep_findings, sweep_reports = [], []
        spell = SpellScan()
        consistency = ConsistencyReport()
        consistency_findings = []
        adjudicate_candidates = []
        story_sheet = ""

    return Prepared(pkg=pkg, doc=doc, chunks=chunks, groups=groups,
                    pass_plan=pass_plan, fmt=fmt,
                    sweep_findings=sweep_findings, sweep_reports=sweep_reports,
                    spell=spell, normalization=norm, baseline=baseline,
                    variant=variant, consistency=consistency,
                    consistency_findings=consistency_findings,
                    adjudicate_candidates=adjudicate_candidates,
                    story_sheet=story_sheet,
                    whole_document=whole,
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
                    conventions: str = "",
                    story: str = "") -> list[Analyzer]:
    return [Analyzer(cfg, group, provider, finding_ids, vocabulary, conventions,
                     story)
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
             coverage=None, provider_factory=None, on_phase=None
             ) -> tuple[list, Usage]:
    """Review every chunk now, one API call per (pass, chunk, detector).

    In single-detector mode (the default) that is one call per (pass, chunk),
    exactly as before. When the ensemble is on, every detector reviews every
    chunk; the calls are still independent, so up to `cfg.concurrency_for()` are
    in flight at once, and results are folded back in strict (pass, chunk, detector)
    order — the order the id counter, the token accounting and the resumable
    checkpoint all depend on. So a big manuscript comes back far sooner,
    byte-for-byte the same as if it had run serially.

    `provider` is the single detector's client in legacy mode; when the ensemble
    is on it is ignored and `provider_factory` (default `build_provider`) builds
    one client per detector model up front, so a missing key fails before any
    call rather than mid-run.

    `progress` is called with (done, total) as each call is folded in, in order.

    `on_phase`, if given, is called with a short stage id each time the run moves
    to a new step — "reviewing" for the per-chunk detector loop, then
    "glossary" / "adjudicate" / "rewrite" / "languagetool" as each whole-document
    pass that is actually enabled begins. It lets a caller show which step the
    document is at, since those post-loop passes emit no per-call progress and
    would otherwise leave the bar frozen at the end of the detector loop.

    `checkpoint` (a docproof.checkpoint.Checkpoint, already loaded) makes the
    run resumable: each completed call's findings land in it as they arrive, in
    order, and calls it already holds are replayed instead of paid for again.

    `should_cancel`, if given, is polled once per folded call; when it first
    returns true the run raises `JobCancelled` and cancels every call not yet
    started, so an abort stops paying for the tail of the document. Calls
    already in flight (at most `cfg.concurrency_for()`) still finish — a thread
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
            dcfg, prepared.pass_types, dprov, ids,
            prepared.vocabulary, prepared.conventions, prepared.story_sheet))

    # The work list, in the order results must be folded back in: pass, then
    # chunk, then detector. With one detector this is (pass, chunk), the legacy
    # order and legacy keys. A pass reads its OWN chunk set (a custom-budget or
    # repeated category differs from the default), so iterate the plan, not a
    # single shared chunk list.
    plan = prepared.effective_pass_plan
    work = []
    for prun in plan:
        for chunk in prun.chunks:
            for d in range(len(specs)):
                work.append((det_analyzers[d][prun.index], chunk,
                             _ckpt_key(prun.index, chunk.chunk_id, d), d,
                             prun.index))

    usage = Usage()
    findings: list = []
    total = prepared.request_count
    # Coverage is per (pass, chunk), not per call: a section is reviewed if ANY
    # detector answered for it, and a gap only when every detector failed it.
    covered: dict = {}

    if on_phase:
        on_phase("reviewing")
    # One pool serves every detector, and with the ensemble on those can sit at
    # different vendors — so the width is the narrowest any of them allows. Take
    # the widest instead and a two-detector run would drive the more cautious
    # vendor at the other's rate. Single-detector mode asks about api.model and
    # gets exactly that model's number.
    width = min(cfg.concurrency_for(model) for model, _effort in specs)
    with ThreadPoolExecutor(max_workers=width) as pool:
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

    # Two post-loop passes, run once (not per-chunk) after the detectors. The
    # glossary reads the whole book with its own (stronger) model; its casing
    # drift is asked, and its suspected misspellings join the adjudication
    # candidates. The adjudication pass then rules on every candidate in context.
    # Every batched pass below reports how many of its candidates actually came
    # back with a verdict. A window whose answer hit the token ceiling carries
    # NO verdicts at all, and without this its candidates would be
    # indistinguishable from ones the model looked at and kept.
    window_losses: list = []
    glossary_cands: list = []
    if cfg.glossary.enabled and prepared.whole_document:
        if on_phase:
            on_phase("glossary")
        from .glossary import (build_glossary, case_drift_findings,
                               suspects_to_candidates)
        gcfg = cfg.model_copy(deep=True)
        gcfg.api.model = cfg.glossary.model
        gcfg.api.effort = cfg.glossary.effort
        glossary = build_glossary(
            prepared.doc.paragraphs, provider_factory(gcfg),
            model=cfg.glossary.model,
            max_tokens=cfg.glossary.max_output_tokens, usage=usage,
            effort=cfg.glossary.effort,
            cache_dir=cache_dir_for(cfg.glossary.cache_dir),
            on_degraded=(
                (lambda reason: coverage.record_degraded("glossary read", reason))
                if coverage is not None else None))
        glossary_cands = suspects_to_candidates(glossary, prepared.doc.paragraphs)
        if cfg.glossary.case_drift:
            findings.extend(case_drift_findings(
                glossary, prepared.doc.paragraphs, ids,
                scan=cfg.glossary.case_drift_scan,
                edit_dominance=cfg.glossary.case_edit_dominance,
                edit_min_count=cfg.glossary.case_edit_min_count))

    # The external-world read: one call, queries only. Whole-document only,
    # like every pass that claims to have read the book.
    if cfg.factcheck.enabled and prepared.whole_document:
        if on_phase:
            on_phase("factcheck")
        from .factcheck import factcheck_findings
        findings.extend(factcheck_findings(
            cfg, prepared.doc.paragraphs, usage, provider_factory))

    if cfg.adjudicate.enabled and (prepared.adjudicate_candidates or glossary_cands):
        if on_phase:
            on_phase("adjudicate")
        from .adjudicate import adjudicate, merge_candidates
        adj_provider = provider if provider is not None else provider_factory(cfg)
        # The deterministic generator's suggestions are listed first, so on a
        # shared site its (dictionary-checked) correction wins over the glossary's.
        cands = merge_candidates(prepared.adjudicate_candidates, glossary_cands)
        findings.extend(adjudicate(
            cands, prepared.doc.paragraphs,
            adj_provider, model=cfg.api.model,
            max_tokens=cfg.api.max_output_tokens, usage=usage, ids=ids,
            batch_size=cfg.adjudicate.batch_size,
            edit_confidence=cfg.adjudicate.edit_confidence,
            loss_sink=window_losses,
            concurrency=cfg.concurrency_for()))

    # Rewrite-then-diff: retype each paragraph minimal-edit, diff for candidates,
    # confirm each in context. Its own model, so a cheap rewriter can run under a
    # dearer detector. Whole-document only, like the two passes above.
    if cfg.rewrite.enabled and prepared.whole_document:
        if on_phase:
            on_phase("rewrite")
        from .rewrite import confirm, propose
        rcfg = cfg.model_copy(deep=True)
        rcfg.api.model = cfg.rewrite.model or cfg.api.model
        rcfg.api.effort = cfg.rewrite.effort
        rw_provider = provider_factory(rcfg)
        rcands = propose(
            prepared.chunks, rw_provider, model=rcfg.api.model,
            max_tokens=cfg.rewrite.max_output_tokens, usage=usage,
            max_add=cfg.rewrite.max_added, max_span=cfg.rewrite.max_span,
            workers=cfg.rewrite.workers, samples=cfg.rewrite.samples,
            diverse=cfg.rewrite.diverse, loss_sink=window_losses)
        confirm_provider, confirm_model = rw_provider, rcfg.api.model
        if cfg.rewrite.confirm_model:
            ccfg = cfg.model_copy(deep=True)
            ccfg.api.model = cfg.rewrite.confirm_model
            ccfg.api.effort = cfg.rewrite.confirm_effort
            confirm_provider = provider_factory(ccfg)
            confirm_model = cfg.rewrite.confirm_model
        findings.extend(confirm(
            rcands, prepared.doc.paragraphs, confirm_provider, model=confirm_model,
            max_tokens=cfg.rewrite.max_output_tokens, usage=usage, ids=ids,
            batch_size=cfg.rewrite.batch_size,
            edit_confidence=cfg.rewrite.edit_confidence,
            loss_sink=window_losses,
            concurrency=cfg.concurrency_for(confirm_model)))

    # LanguageTool mechanical-floor pass: a LOCAL rules checker proposes commas,
    # missing words, and hyphenation the model glides past; the shared confirm
    # valve rules on each so nothing is edited blind. No API cost of its own —
    # only the light confirm calls. Whole-document only (it reads the lexicon).
    if cfg.languagetool.enabled and prepared.whole_document:
        if on_phase:
            on_phase("languagetool")
        from .languagetool import (all_disabled_rules, propose as lt_propose,
                                   shutdown as lt_shutdown)
        from .rewrite import confirm as lt_confirm
        try:
            lt_cands = lt_propose(
                prepared.doc.paragraphs, lexicon=prepared.spell.lexicon,
                dictionary=cfg.languagetool.dictionary,
                disabled_rules=all_disabled_rules(cfg.languagetool.disabled_rules),
                workers=cfg.languagetool.workers, progress=progress)
            lt_provider, lt_model = provider, cfg.api.model
            if cfg.languagetool.confirm_model:
                lcfg = cfg.model_copy(deep=True)
                lcfg.api.model = cfg.languagetool.confirm_model
                lcfg.api.effort = cfg.languagetool.confirm_effort
                lt_provider = provider_factory(lcfg)
                lt_model = cfg.languagetool.confirm_model
            findings.extend(lt_confirm(
                lt_cands, prepared.doc.paragraphs, lt_provider, model=lt_model,
                max_tokens=cfg.languagetool.max_output_tokens, usage=usage, ids=ids,
                batch_size=cfg.languagetool.batch_size,
                edit_confidence=cfg.languagetool.edit_confidence,
                error_type="languagetool", chunk_id="languagetool", id_prefix="lt",
                loss_sink=window_losses,
                concurrency=cfg.concurrency_for(lt_model)))
        finally:
            lt_shutdown()

    # Whole-book continuity read. Shared with the batch collector rather than
    # written out here, so the two paths cannot drift apart — see
    # continuity_findings.
    findings.extend(continuity_findings(cfg, prepared, ids, usage,
                                        provider_factory, on_phase=on_phase,
                                        coverage=coverage))

    if coverage is not None:
        coverage.record_windows(window_losses)

    return findings, usage


def continuity_findings(cfg: Config, prepared: Prepared, ids, usage: Usage,
                        provider_factory, *, on_phase=None,
                        coverage=None) -> list[Finding]:
    """The whole-book continuity read's findings, or [] when the pass is off.

    One frontier read of the whole manuscript for facts it contradicts about
    itself — timeline slips, age/date arithmetic, attribute drift, object
    continuity — the class the chunked detectors are structurally blind to (they
    never see two distant passages together). Every finding is force_query: a
    margin comment, never an edit, because which fact is right is the author's
    call. A deterministic date->weekday check rides along at no API cost.
    Additive and best-effort like the glossary; whole-document only. See
    docproof/continuity.py.

    A function rather than a block inside `run_sync` because the read has to
    happen on BOTH paths. It is one synchronous whole-book call on its own model,
    so it cannot ride a review batch (a batch is one model, and this is a single
    request) — meaning a batch review has to make it at collect time, exactly as
    it does the glossary read. It lived only in `run_sync` for a while, and since
    batch is the app's default submission mode, the common path was reporting and
    pricing a read it never made."""
    if not (cfg.continuity.enabled and prepared.whole_document):
        return []
    if on_phase:
        on_phase("continuity")
    from .continuity import (build_continuity, calendar_findings,
                             report_to_findings)
    from .utils.tokens import estimate_tokens

    out: list[Finding] = []
    doc_tokens = sum(estimate_tokens(p.text) for p in prepared.doc.paragraphs)
    if doc_tokens > cfg.continuity.max_input_tokens:
        log.warning("continuity: ~%d tokens over max_input_tokens %d — "
                    "skipping the read to avoid a truncated (silently "
                    "incomplete) one; the calendar check still runs",
                    doc_tokens, cfg.continuity.max_input_tokens)
    else:
        ccfg = cfg.model_copy(deep=True)
        ccfg.api.model = cfg.continuity.model
        ccfg.api.effort = cfg.continuity.effort
        report = build_continuity(
            prepared.doc.paragraphs, provider_factory(ccfg),
            model=cfg.continuity.model,
            max_tokens=cfg.continuity.max_output_tokens, usage=usage,
            prompt=cfg.continuity.prompt,
            effort=cfg.continuity.effort,
            cache_dir=cache_dir_for(cfg.continuity.cache_dir),
            on_degraded=(
                (lambda reason: coverage.record_degraded("continuity read", reason))
                if coverage is not None else None))
        out.extend(report_to_findings(
            report, prepared.doc.paragraphs, ids,
            min_confidence=cfg.continuity.min_confidence,
            max_queries=cfg.continuity.max_queries))
    if cfg.continuity.calendar_check:
        out.extend(calendar_findings(prepared.doc.paragraphs, ids))
    return out


def _sapling_findings(cfg: Config, prepared: Prepared,
                      usage: Usage | None = None, *,
                      out_dir: str | Path | None = None,
                      loss_sink: list | None = None) -> list[Finding]:
    """Sapling's suggestions as findings, or [] when the pass is off, has no key,
    or the service can't be reached.

    With `sapling.confirm` on (the default), every Sapling edit is routed through
    the SHARED rewrite.confirm valve: an LLM rules on each in literary context and
    keeps anything that would touch voice, dialect, invented names, or style, so
    Sapling never edits blind. Its `describe()` line rides along as the margin
    comment, and a softer-than-`edit_confidence` affirmation becomes a margin
    query rather than a silent change.

    With `confirm` off, each edit is instead turned into the same quoted-sentence
    finding a sweep produces (via `sentence_window`), gated only by the
    deterministic validation/overlap/edit-guard downstream — the older behaviour,
    kept so a run can A/B raw vs valved.

    Either way a Sapling edit landing on a span a house-style sweep already
    claimed is dropped for free, because sweeps validate ahead of it. Runs here in
    `finish`, once per review, rather than in `prepare` (which runs twice in batch
    mode) — Sapling is a paid network call, not a free local scan. A failure
    degrades to no findings and a warning, never a failed review."""
    if not cfg.sapling.enabled:
        return []
    import os

    from .sapling import (SaplingError, check_paragraphs, describe,
                          to_candidates)
    from .sweeps import sentence_window
    doc = prepared.doc
    key = os.environ.get("SAPLING_API_KEY")
    if not key:
        log.warning("Sapling pass is on but SAPLING_API_KEY is not set — "
                    "skipping it.")
        return []
    try:
        edits = check_paragraphs(
            [(p.para_id, p.text) for p in doc.paragraphs], key,
            variety=cfg.sapling.variety or None)
    except SaplingError as e:
        # A pass that never returned bills nothing here — the cost stays 0 rather
        # than charging a person for a book Sapling couldn't read.
        log.warning("Sapling pass failed (%s); continuing without it.", e)
        return []

    # Bill for what was sent — the non-blank paragraph text, exactly what
    # check_paragraphs submits — so the reported cost is the whole bill, not just
    # the model's share, and matches the estimate shown before the run.
    if usage is not None:
        chars = sum(len(p.text) for p in doc.paragraphs if p.text.strip())
        usage.sapling_chars = chars
        usage.sapling_cost = chars * cfg.sapling.cost_per_1k_chars / 1000

    if cfg.sapling.confirm:
        from itertools import count

        from .providers import build_provider
        from .rewrite import confirm as sap_confirm
        para_text = {p.para_id: p.text for p in doc.paragraphs}
        cands = to_candidates(
            edits, para_text, lexicon=prepared.spell.lexicon,
            disabled_error_types=cfg.sapling.disabled_error_types)
        if not cands:
            return []
        provider = build_provider(cfg)
        model = cfg.api.model
        if cfg.sapling.confirm_model:
            scfg = cfg.model_copy(deep=True)
            scfg.api.model = cfg.sapling.confirm_model
            scfg.api.effort = cfg.sapling.confirm_effort
            provider = build_provider(scfg)
            model = cfg.sapling.confirm_model
        rejected: list = []
        findings = sap_confirm(
            cands, doc.paragraphs, provider, model=model,
            max_tokens=cfg.sapling.max_output_tokens,
            usage=usage if usage is not None else Usage(), ids=count(1),
            batch_size=cfg.sapling.batch_size,
            edit_confidence=cfg.sapling.edit_confidence,
            reject_sink=rejected, loss_sink=loss_sink,
            error_type="sapling", chunk_id="sapling",
            id_prefix="sap", silent=not cfg.sapling.comments,
            concurrency=cfg.concurrency_for(model))
        # The valve's rejections are where a real Sapling catch can die — persist
        # them so a run can measure raw -> survivors and see what the 5% was.
        if out_dir is not None and rejected:
            import json
            try:
                Path(out_dir, "sapling_rejected.json").write_text(
                    json.dumps(rejected, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:                     # debug artifact, never fatal
                log.warning("Sapling: could not write reject log: %s", e)
        log.info("Sapling: %d confirmed edit(s) from %d candidate(s) "
                 "(%d kept as not-an-error).",
                 len(findings), len(cands), len(rejected))
        return findings

    paras = {p.para_id: p for p in doc.paragraphs}
    findings: list[Finding] = []
    n = 0
    for e in edits:
        para = paras.get(e.para_id)
        if para is None:
            continue
        text = para.text
        if text[e.start:e.end] != e.original:      # offset drift — never edit blind
            continue
        window, lo, occurrence = sentence_window(text, e.start, e.end)
        corrected = window[:e.start - lo] + e.replacement + window[e.end - lo:]
        if corrected == window:                    # a no-op suggestion
            continue
        n += 1
        findings.append(Finding(
            finding_id=f"sap-{n:04d}",
            chunk_id="sapling",
            para_id=e.para_id,
            error_type="sapling",
            original_text=window,
            occurrence=occurrence,
            corrected_text=corrected,
            explanation=describe(e),
            confidence="high",
            silent=not cfg.sapling.comments,
        ))
    log.info("Sapling proposed %d edit(s) across %d paragraph(s).",
             len(findings), len(doc.paragraphs))
    return findings


def _smoothing_findings(cfg: Config, prepared: Prepared,
                        usage: Usage | None = None, *,
                        out_dir: str | Path | None = None):
    """The line-editing pass's suggestions, as margin queries, or [] when off.

    Two paid calls: a line editor's read of the manuscript, then a skeptical
    taste judge over what it proposed. Between them sit the deterministic
    filters (dialogue, the author's lexicon, the minimal-edit scale) — cheap and
    absolute, so the judge is only ever asked about candidates that already
    cleared the rules the pass must never break.

    Reuses the shared confirm valve for the judging, in `mode="suggestion"`:
    same batching, same abort semantics, same reject log as every other
    candidate source, but every finding comes back force_query'd. See
    docproof/smoothing.py for why that is structural rather than a threshold.

    Returns the capped findings and a SmoothingReport, because the number of
    suggestions the cap withheld is not recoverable from the findings that
    survived it — and going unreported is the one thing a cap must not do."""
    from .smoothing import (JUDGE_SYSTEMS, PROPOSE_SYSTEM, PROPOSE_SYSTEM_OPEN,
                            SmoothingReport, cap_for, propose, prompt_sha,
                            rank_and_cap)
    # Whole-document only, like every other pass that reads the book entire. A
    # selected-sections run must not buy a full-manuscript line edit, and — the
    # part that would be visible to the author — must not come back with
    # "Consider: ..." in chapters nobody asked anyone to look at.
    if not (cfg.smoothing.enabled and prepared.whole_document):
        if cfg.smoothing.enabled:
            log.info("Smoothing: skipped — a selected-sections run does not buy "
                     "a whole-book line edit.")
        return [], SmoothingReport()

    from itertools import count

    from .providers import build_provider
    from .rewrite import confirm as smooth_confirm
    sm = cfg.smoothing
    doc = prepared.doc
    usage = usage if usage is not None else Usage()

    pcfg = cfg.model_copy(deep=True)
    pcfg.api.model = sm.model or cfg.api.model
    pcfg.api.effort = sm.effort          # applies whether or not `model` is set
    propose_model = pcfg.api.model
    # Resolve the proposer prompt ONCE and use the same string everywhere — the
    # propose call AND every fingerprint below. An explicit propose_prompt wins;
    # otherwise proposer_restraint (C1) chooses between the shipped prompt and
    # the de-restrained PROPOSE_SYSTEM_OPEN. Resolving once is what keeps the
    # recorded propose_prompt_sha honest: if the prompt were re-derived at the
    # sha sites, a run could send one prompt and fingerprint another, and the
    # eval — which keys comparability off the sha — would silently fold two
    # incomparable runs together.
    propose_system = sm.propose_prompt or (
        PROPOSE_SYSTEM_OPEN if sm.proposer_restraint == "open"
        else PROPOSE_SYSTEM)
    cands, filtered, n_windows, windows_failed = propose(
        doc.paragraphs, build_provider(pcfg), model=propose_model,
        max_tokens=sm.max_output_tokens, usage=usage,
        system=propose_system,
        lexicon=prepared.spell.lexicon,
        # A U.K. manuscript closes dialogue with the mark a U.S. one uses for a
        # quote inside one, so the dialogue skip has to know which it is. No
        # variant (a bare Prepared) falls back to the double-quote reading.
        closing_quotes=(prepared.variant.closing_quotes
                        if prepared.variant else "”\""),
        include_dialogue=sm.include_dialogue, edit_guard=cfg.edit_guard,
        concurrency=cfg.concurrency_for(propose_model),
        propose_chars=sm.propose_chars, propose_max_paras=sm.propose_max_paras,
        dialogue_categories=sm.dialogue_categories)
    if windows_failed:
        # The propose-side twin of the unjudged warning below. A read that
        # truncated dropped a whole window of the manuscript, which shows up only
        # as fewer suggestions unless it is said out loud.
        log.warning("Smoothing: %d of %d reading pass(es) came back truncated or "
                    "unreadable, so those parts of the manuscript went unread — "
                    "treat this run's volume as a floor. Raising "
                    "smoothing.max_output_tokens is the fix if it recurs.",
                    windows_failed, n_windows)
    if not cands:
        # Provenance even on the empty path. A run that proposed nothing and a
        # run that never happened produce the same findings — and on this pass
        # silence is the ordinary output, so the difference has to be recorded
        # rather than inferred. `propose_model` is what says the manuscript was
        # actually read; see docproof/labels.py. `windows_failed` distinguishes a
        # genuinely quiet read from one where every window truncated.
        return [], SmoothingReport(
            filtered=filtered, windows=n_windows, windows_failed=windows_failed,
            propose_model=propose_model, judge_model=sm.judge_model,
            propose_prompt_sha=prompt_sha(propose_system))

    # The judge reads with the same book knowledge the round judge gets. Folded
    # into its system prompt rather than passed separately: the confirm valve's
    # user message is the numbered items and nothing else, which is what lets
    # every candidate source share one batching path.
    context = "\n\n".join(x for x in (prepared.conventions, prepared.vocabulary,
                                      prepared.story_sheet) if x)
    # Judge prompt resolved once too. C4's judge_harshness selects a rung of the
    # JUDGE_SYSTEMS dial ("strict" is the shipped prompt); an explicit judge_prompt
    # still wins. The context is folded in after, and judge_prompt_sha below
    # fingerprints the whole thing.
    system = sm.judge_prompt or JUDGE_SYSTEMS[sm.judge_harshness]
    if context:
        system = f"{system}\n\n{context}"
    jcfg = cfg.model_copy(deep=True)
    jcfg.api.model = sm.judge_model
    jcfg.api.effort = sm.judge_effort
    rejected: list = []
    judged = smooth_confirm(
        cands, doc.paragraphs, build_provider(jcfg), model=sm.judge_model,
        max_tokens=sm.judge_max_output_tokens, usage=usage, ids=count(1),
        batch_size=sm.batch_size, reject_sink=rejected,
        error_type="smoothing", chunk_id="smoothing", id_prefix="sm",
        concurrency=cfg.concurrency_for(sm.judge_model),
        system=system, mode="suggestion")

    # Every candidate should come back either affirmed or in the reject log. Any
    # that did neither were in a batch the judge failed to answer — a truncated
    # or unparseable reply drops the whole window silently, and the run would
    # otherwise report the loss as restraint.
    unjudged = max(0, len(cands) - len(judged) - len(rejected))
    if unjudged:
        log.warning("Smoothing: the judge never ruled on %d of %d candidate(s) "
                    "— a batch reply was truncated or unusable. Treat this "
                    "run's volume as a floor, not a measurement.",
                    unjudged, len(cands))

    words = sum(len(p.text.split()) for p in doc.paragraphs)
    cap = cap_for(words, sm.max_per_1000_words)
    kept, withheld, below_floor = rank_and_cap(judged, cap, sm.min_confidence)
    # The judge's rejections are the pass's own taste record: what a strong model
    # thought was not worth the author's attention. Persisted for the same reason
    # Sapling's are — it is the only way to measure the valve rather than guess.
    if out_dir is not None and rejected:
        import json
        try:
            Path(out_dir, "smoothing_rejected.json").write_text(
                json.dumps(rejected, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:                      # debug artifact, never fatal
            log.warning("Smoothing: could not write reject log: %s", e)
    log.info("Smoothing: %d suggestion(s) from %d candidate(s) "
             "(%d filtered before the judge, %d kept as not-worth-raising, "
             "%d withheld by the cap of %d).",
             len(kept), len(cands), filtered, len(rejected), withheld, cap)
    return kept, SmoothingReport(
        proposed=len(cands), kept=len(kept), withheld=withheld, cap=cap,
        unjudged=unjudged, filtered=filtered,
        refused=len(rejected), below_floor=below_floor,
        windows=n_windows, windows_failed=windows_failed,
        propose_model=propose_model, judge_model=sm.judge_model,
        propose_prompt_sha=prompt_sha(propose_system),
        judge_prompt_sha=prompt_sha(system))


def _chapter_continuity_findings(cfg: Config, prepared: Prepared,
                                 usage: Usage | None = None, *,
                                 out_dir: str | Path | None = None,
                                 book_anchors: frozenset[tuple[str, str]] = frozenset(),
                                 batch_reads: dict | None = None):
    """The chapter-scoped continuity read's queries and a ChapterContinuityReport,
    or [] when off. The third reading distance: the class of break that closes
    inside one chapter and is invisible to both the per-paragraph detectors and
    the whole-book read.

    `batch_reads`, when given, is the chapter-read batch's results keyed by
    custom_id (docproof/batch.py) — the propose stage rode a batch for the 0.5x
    discount, so its reads are parsed here rather than bought synchronously. None
    (the sync path, and any run where the reads did not ride a batch) reads each
    chapter live. The judge always runs here, synchronously, either way.

    Two paid stages, like smoothing: a per-chapter read that proposes in-scene
    breaks, then a skeptical judge over what survives the deterministic filters
    (both quotes in the same chapter; no break the whole-book read already asked,
    via `book_anchors`). Every finding is force_query'd, and the volume is capped
    per chapter. Runs in finish(), so it happens exactly once per book even under
    multi-round — the same reason smoothing lives here rather than on the two
    per-round pre-finish paths. Whole-document only, like every whole-book pass.

    Returns the findings and a report because the number the cap withheld and the
    number the judge never ruled on are not recoverable from the findings that
    survived, and on this pass silence is the ordinary output — so the difference
    between a restrained run and a failed one has to be recorded, not inferred."""
    from .continuity import (ChapterContinuityReport, breaks_to_findings,
                             chapter_reads_from_batch, chapters,
                             default_chapter_continuity_prompt,
                             judge_chapter_breaks, propose_chapter_breaks)
    cc = cfg.chapter_continuity
    if not (cc.enabled and prepared.whole_document):
        if cc.enabled:
            log.info("Chapter continuity: skipped — a selected-sections run does "
                     "not buy a whole-book chapter read.")
        return [], ChapterContinuityReport()

    from itertools import count

    from .providers import build_provider
    from .smoothing import prompt_sha
    usage = usage if usage is not None else Usage()

    # The chapter reader's model: its own, else the whole-book read's, else the
    # detector's. Per-chapter reading is easier than whole-book needle-finding, so
    # a cheaper model may earn its place here.
    propose_model = cc.model or cfg.continuity.model or cfg.api.model
    # Fingerprint the RESOLVED prompt: a whitespace-only override is stripped away
    # at the point of use (propose_chapter_breaks does `system.strip() or default`)
    # so hashing the raw value would record a prompt that never ran and mark two
    # equivalent runs as non-comparable.
    propose_prompt = cc.prompt.strip() or default_chapter_continuity_prompt()

    units = chapters(prepared.doc.paragraphs, cfg.skip.is_sweep_only,
                     min_tokens=cc.min_chapter_tokens,
                     max_tokens=cc.max_chapter_tokens)
    if not units:
        return [], ChapterContinuityReport(
            propose_model=propose_model, judge_model=cc.judge_model,
            propose_prompt_sha=prompt_sha(propose_prompt))
    if not any(u.title for u in units):
        log.info("Chapter continuity: no chapter markers found; reading in "
                 "%d book-sized window(s) instead.", len(units))
    else:
        log.info("Chapter continuity: segmented into %d chapter(s).", len(units))

    if batch_reads is not None:
        # The reads rode a batch (docproof/batch.py). Parse them here rather than
        # buying them live; the units re-segment identically off the unchanged
        # document, so the custom_ids map back by index.
        cands, filtered, read_failed = chapter_reads_from_batch(
            units, batch_reads, usage, book_anchors)
    else:
        pcfg = cfg.model_copy(deep=True)
        pcfg.api.model = propose_model
        pcfg.api.effort = cc.effort
        cands, filtered, read_failed = propose_chapter_breaks(
            units, build_provider(pcfg), model=propose_model,
            max_tokens=cc.max_output_tokens, usage=usage, system=cc.prompt,
            effort=cc.effort, cache_dir=cache_dir_for(cc.cache_dir),
            concurrency=cfg.concurrency_for(propose_model),
            book_anchors=book_anchors)
    if not cands:
        return [], ChapterContinuityReport(
            chapters=len(units), read_failed=read_failed, filtered=filtered,
            propose_model=propose_model, judge_model=cc.judge_model,
            propose_prompt_sha=prompt_sha(propose_prompt))

    # The "how hard it looks" dial picks the judge's posture and the confidence
    # floor together; a non-empty judge_prompt override still wins over the posture.
    from .continuity import sensitivity_profile
    base_judge, floor = sensitivity_profile(cc.sensitivity)
    # The judge reads with the same book knowledge the smoothing judge gets —
    # folded into its system prompt, since a per-chapter read lacks the context a
    # whole-book one has, and the story sheet is the cheap stand-in (e.g. a book
    # whose nonlinear structure is established, not a break).
    context = "\n\n".join(x for x in (prepared.conventions, prepared.vocabulary,
                                      prepared.story_sheet) if x)
    judge_base = cc.judge_prompt.strip() or base_judge
    full_judge_system = f"{judge_base}\n\n{context}" if context else judge_base

    jcfg = cfg.model_copy(deep=True)
    jcfg.api.model = cc.judge_model
    jcfg.api.effort = cc.judge_effort
    rejected: list = []
    affirmed, unjudged = judge_chapter_breaks(
        cands, build_provider(jcfg), model=cc.judge_model,
        max_tokens=cc.judge_max_output_tokens, usage=usage,
        batch_size=cc.batch_size, system=full_judge_system,
        reject_sink=rejected, concurrency=cfg.concurrency_for(cc.judge_model))

    findings, withheld, below_floor = breaks_to_findings(
        affirmed, count(1), min_confidence=floor,
        max_per_chapter=cc.max_per_chapter)

    # The judge's rejections are this pass's own taste record — what a strong model
    # thought not worth the author's attention. Persisted for the same reason
    # smoothing's are: the only way to tune the judge rather than guess.
    if out_dir is not None and rejected:
        import json
        try:
            Path(out_dir, "chapter_continuity_rejected.json").write_text(
                json.dumps(rejected, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:                          # debug artifact, never fatal
            log.warning("Chapter continuity: could not write reject log: %s", e)
    log.info("Chapter continuity: %d query/queries from %d candidate(s) across "
             "%d chapter(s) (%d filtered pre-judge, %d refused by the judge, "
             "%d withheld by the per-chapter cap of %d).",
             len(findings), len(cands), len(units), filtered, len(rejected),
             withheld, cc.max_per_chapter)
    return findings, ChapterContinuityReport(
        chapters=len(units), read_failed=read_failed, proposed=len(cands),
        kept=len(findings), withheld=withheld, cap=cc.max_per_chapter,
        unjudged=unjudged, filtered=filtered, refused=len(rejected),
        below_floor=below_floor, propose_model=propose_model,
        judge_model=cc.judge_model,
        propose_prompt_sha=prompt_sha(propose_prompt),
        judge_prompt_sha=prompt_sha(full_judge_system))


def _promote_low_confidence(cfg: Config, prepared: Prepared,
                            model_findings: list[Finding], usage: Usage, *,
                            out_dir: str | Path | None = None,
                            loss_sink: list | None = None
                            ) -> tuple[list[Finding], list[Finding]]:
    """Give a below-`min_confidence` model EDIT one more chance to be applied.

    Each below-gate edit is turned into a RewriteCandidate and run through the
    SHARED rewrite.confirm valve — an LLM rules on it in literary context — so a
    genuine dialogue-mechanics catch (which the type prompts mark "low" on
    purpose) can be PROMOTED to a tracked change instead of only ever surfacing
    as a margin comment. A softer affirmation becomes a margin query; a "not an
    error" verdict drops it. Precision is restored downstream rather than at the
    gate, so the gate itself does not have to be lowered.

    Returns `(survivors, promoted)`. `survivors` is `model_findings` with the
    below-gate edits removed but everything else untouched — above-gate findings,
    queries, formatting marks, and any below-gate edit that could not be anchored
    or shrank to a no-op (those are left in place so the validator reports them as
    it always has, never silently dropped). `promoted` is the valve's verdicts,
    which the caller validates LAST so they yield to every surer source on a span.

    Off unless `low_confidence.confirm`; a paid pass, so measure before shipping
    on. The valve's rejections persist to `low_confidence_rejected.json` so a run
    can see what the below-gate promotion recovered and what it correctly let go."""
    if not cfg.low_confidence.confirm:
        return model_findings, []
    from itertools import count

    from .models import CONFIDENCE_RANK
    from .providers import build_provider
    from .rewrite import RewriteCandidate
    from .rewrite import confirm as lc_confirm
    from .validator import anchor_offset, shrink

    gate = CONFIDENCE_RANK[cfg.min_confidence]
    para_text = {p.para_id: p.text for p in prepared.doc.paragraphs}
    survivors: list[Finding] = []
    cands: list[RewriteCandidate] = []
    for f in model_findings:
        below = CONFIDENCE_RANK[f.confidence] < gate
        # Only genuine edits enter the valve. A query, a forced query, and a
        # formatting mark are not tracked changes and never touched the gate;
        # anything already at or above the gate is applied as usual.
        editable = (f.error_type not in prepared.query_types
                    and not f.force_query
                    and f.error_type not in prepared.format_types)
        text = para_text.get(f.para_id)
        if not (below and editable) or text is None:
            survivors.append(f)
            continue
        s = anchor_offset(text, f.original_text, f.occurrence)
        if s == -1:
            survivors.append(f)                  # let it report as no-anchor
            continue
        pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
        if not deleted and not inserted:
            survivors.append(f)                  # no-op, handled downstream
            continue
        start, end = s + pre, s + pre + len(deleted)
        if text[start:end] != deleted:           # offset drift after folding
            survivors.append(f)
            continue
        cands.append(RewriteCandidate(
            para_id=f.para_id, start=start, end=end,
            original=deleted, replacement=inserted, note=f.explanation or None))
    if not cands:
        return survivors, []

    lc = cfg.low_confidence
    provider = build_provider(cfg)
    model = cfg.api.model
    if lc.confirm_model:
        scfg = cfg.model_copy(deep=True)
        scfg.api.model = lc.confirm_model
        scfg.api.effort = lc.confirm_effort
        provider = build_provider(scfg)
        model = lc.confirm_model
    rejected: list = []
    promoted = lc_confirm(
        cands, prepared.doc.paragraphs, provider, model=model,
        max_tokens=lc.max_output_tokens, usage=usage, ids=count(1),
        batch_size=lc.batch_size, edit_confidence=lc.edit_confidence,
        reject_sink=rejected, loss_sink=loss_sink,
        error_type="low_confidence",
        chunk_id="low_confidence", id_prefix="lc",
        concurrency=cfg.concurrency_for(model))
    if out_dir is not None and rejected:
        import json
        try:
            Path(out_dir, "low_confidence_rejected.json").write_text(
                json.dumps(rejected, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:                     # debug artifact, never fatal
            log.warning("Low-confidence: could not write reject log: %s", e)
    log.info("Low-confidence: %d promoted from %d below-gate candidate(s) "
             "(%d kept as not-an-error).",
             len(promoted), len(cands), len(rejected))
    return survivors, promoted


MEANING_HELD_FILE = "meaning_held.json"


def read_meaning_held(out_dir) -> list[dict]:
    """The changes an earlier run's judge gates held back, if it recorded any.

    A replay (a "download anyway" rebuild, or a re-judge of a finished run) has
    to reproduce those verdicts without paying for them again — the alternative
    is handing the author the un-gated document their summary told them they were
    protected from. The file keeps its original name so a rebuild of a job run
    before the second gate existed still reads."""
    import json
    p = Path(out_dir, MEANING_HELD_FILE)
    try:
        rows = json.loads(p.read_text("utf-8"))
        return rows if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _write_judge_held(out_dir, rows) -> None:
    import json
    try:
        Path(out_dir, MEANING_HELD_FILE).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:                     # never fatal to a finished review
        log.warning("Judge gates: could not record held-back changes: %s", e)


def _held_key(f) -> tuple:
    """How a held-back change is recognised again on a replay. Coordinate-free
    and id-free: ids are only unique per source, and offsets shift."""
    return (f.para_id, f.original_text, f.occurrence, f.corrected_text)


def _run_judge_gates(cfg: Config, prepared: Prepared, validated: list,
                     usage: Usage, *, out_dir, replay: list[dict] | None,
                     on_phase=None, coverage=None):
    """Run every enabled judge gate over `validated`, in place.

    Returns (reports, held) — the per-gate reports for the run report, and how
    many changes were withdrawn into the margin. With `replay` supplied and the
    gates off, an earlier run's verdicts are re-applied instead, costing
    nothing — and those count as held too, because the document in the author's
    hands holds them back however the verdict was reached.

    `on_phase` names each gate as it starts ruling — the ids are the gates'
    feature ids, so the app's step tracker matches its switches — and only when
    the gate actually has changes to read, so a run with nothing to judge never
    announces a step that does no work."""
    from .judges import SPECS, screen
    from .validator import to_query

    gates = [("meaning_check", cfg.meaning_check, SPECS["meaning"]),
             ("fix_check", cfg.fix_check, SPECS["fix"])]
    if not any(g.enabled for _, g, _s in gates):
        return [], (_replay_judge_gates(prepared, validated, replay)
                    if replay else 0)

    para_text = {p.para_id: p.text for p in prepared.doc.paragraphs}
    context = "\n\n".join(x for x in (prepared.conventions,
                                      prepared.vocabulary) if x)
    reports, rows = [], []
    for stage_id, gate, spec in gates:
        if not gate.enabled:
            continue
        # Eligibility is a position range, not a set of ids. `proposed` is built
        # in a known order — the deterministic sweeps and the consistency scan
        # first, then everything a model or an outside checker proposed — and
        # validate_findings returns exactly one finding per input, in order, so
        # position identifies a finding exactly where an id would not (ids are
        # only unique per source: continuity and consistency both mint "c-0001").
        first = 0
        skip_deterministic = gate.scope == "model_sources"
        if skip_deterministic:
            first = len(prepared.sweep_findings) + len(prepared.consistency_findings)
        # A query changes no text and a formatting mark changes no characters, so
        # neither has anything for a judge to rule on. Only tracked changes are
        # read — which also means a change an earlier gate withdrew is already
        # out of this list, unbilled and unjudged twice.
        where = [i for i in range(first, len(validated))
                 if validated[i].status == "validated" and not validated[i].format
                 # ...and a tag check on top of the range, because multi-round
                 # hands finish() a composed list with those two source lists
                 # emptied (the sweeps were folded in every round), so the range
                 # alone would put every house-style edit in front of a frontier
                 # model. Both scripted sources stamp their own chunk_id, and
                 # that survives composition.
                 and not (skip_deterministic
                          and validated[i].chunk_id in ("sweep", "consistency"))]
        if not where:
            continue
        if on_phase:
            on_phase(stage_id)
        # Built only now, and only if there is something to judge: this is the
        # far end of a run that has already been paid for, and a client
        # constructed for a manuscript with no changes in it would raise on a
        # missing key and take every output of that run down with it.
        from .providers import build_provider
        gcfg = cfg.model_copy(deep=True)
        gcfg.api.model = gate.model
        gcfg.api.effort = gate.effort
        report = screen(
            [validated[i] for i in where], para_text, build_provider(gcfg),
            spec=spec, model=gate.model, instructions=gate.prompt,
            # The house conventions AND the book's own vocabulary, exactly as the
            # overseer-verifier gets them: without the vocabulary a coined name
            # reads as a word substitution, and a judge would hold back the very
            # corrections around it that are safest.
            context=context, max_tokens=gate.max_output_tokens,
            concurrency=cfg.concurrency_for(gate.model),
            flag_unsure=gate.flag_unsure,
            usage=usage)
        for pos, f in zip(report.positions, report.withheld):
            validated[where[pos]] = to_query(f, prepared.doc)
        rows += [{"judge": spec.key, "para_id": f.para_id,
                  "original_text": f.original_text, "occurrence": f.occurrence,
                  "corrected_text": f.corrected_text,
                  "explanation": f.explanation} for f in report.withheld]
        reports.append(report)
        # A gate that could not read some of what it was given (a dead key, a
        # refusal, a truncated batch) applied those changes unread — the exact
        # silent hole a re-pinned-but-unkeyed judge leaves. Summary.md already
        # names it per gate; this also carries it to the job card.
        if coverage is not None and report.unread:
            coverage.record_degraded(
                f"{stage_id.replace('_', '-')} gate",
                f"{report.unread} change(s) applied unread")

    # Persist what was held back. The verdicts live nowhere else — the checkpoint
    # only carries raw detector output, written long before finish() runs — so
    # without this a "download anyway" replay rebuilds the file with every
    # held-back change applied, handing the author the un-gated document their
    # summary told them they were protected from. See app/jobs.py.
    if rows:
        _write_judge_held(out_dir, rows)
    return reports, len(rows)


def _replay_judge_gates(prepared: Prepared, validated: list,
                        rows: list[dict]) -> int:
    """Re-apply an earlier run's verdicts without paying a judge to reach them.

    Returns how many changes were actually withdrawn — which is not
    `len(rows)`: a row only matches if the finding is still a validated change
    in this rebuild, and the report should say what this document holds back,
    not what the original run recorded."""
    from .validator import to_query
    notes = {(r.get("para_id"), r.get("original_text"), r.get("occurrence"),
              r.get("corrected_text")): r.get("explanation", "") for r in rows}
    n = 0
    for i, f in enumerate(validated):
        if f.status != "validated" or f.format:
            continue
        note = notes.get(_held_key(f))
        if note is None:
            continue
        validated[i] = to_query(replace(f, explanation=note or f.explanation),
                                prepared.doc)
        n += 1
    if n:
        log.info("Judge gates: re-applied %d held-back change(s) from the "
                 "original run.", n)
    return n


def finish(prepared: Prepared, findings: list, usage: Usage, cfg: Config, *,
           out_dir: str | Path, source_path: str | Path,
           batch: bool = False, coverage=None, verify_provider=None,
           judge_held: list[dict] | None = None,
           chapter_batch_reads: dict | None = None, on_phase=None) -> Outputs:
    """Validate, write tracked changes, save, and report.

    `coverage` (a CoverageLedger, if the caller tracked one) records which
    (pass, chunk) units were actually reviewed, so the report can name any that
    the model never answered rather than letting the gap pass silently.

    `verify_provider` is the overseer-verifier's client; when the ensemble
    configures a verifier and none is passed, one is built from
    ensemble.verifier_model. Ignored entirely in single-detector mode.

    `on_phase` is the same callback `run_sync` takes, continued: each paid pass
    in here announces itself as it begins — "verify" / "sapling" /
    "low_confidence" / "smoothing" / "chapter_continuity", then the judge gates
    ("meaning_check" / "fix_check"), then "writing" once the document is
    actually being assembled. Without it everything below used to hide under
    the caller's single "writing" stage, which on a big book meant many minutes
    of judge and whole-book passes with no sign of which was running."""
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
            if on_phase:
                on_phase("verify")
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
    # anything the model reports about the same characters. Sapling sits after
    # the deterministic sweeps and consistency (so a house-style sweep keeps a
    # contested span, and Sapling's overlap with it is dropped) but ahead of the
    # model, which is the fuzzier source on any span the two both touch.
    # Both passes below run the shared confirm valve, so both can lose a window
    # to a token ceiling; the ledger is what keeps that out of the flattering
    # reading (see CoverageLedger.unruled).
    window_losses: list = []
    if on_phase and cfg.sapling.enabled:
        on_phase("sapling")
    sapling_findings = _sapling_findings(cfg, prepared, usage, out_dir=out,
                                         loss_sink=window_losses)
    # Below-gate model edits get one more chance to become a tracked change: the
    # confirm valve re-rules each in literary context, so a real dialogue-
    # mechanics catch the type prompts marked "low" is promoted rather than left
    # to a margin comment. Off by default. `model_findings` comes back with the
    # below-gate edits removed and the valve's verdicts (`promoted`) taking their
    # place; the verdicts go LAST so they yield to every surer source on a span.
    if on_phase and cfg.low_confidence.confirm:
        on_phase("low_confidence")
    model_findings, promoted = _promote_low_confidence(
        cfg, prepared, model_findings, usage, out_dir=out,
        loss_sink=window_losses)
    if coverage is not None:
        coverage.record_windows(window_losses)
    # Smoothing goes LAST, and its position is the least of its safeguards. Every
    # suggestion it emits is force_query'd, and the validator's query branch
    # never claims a span at all — a question and a tracked change may sit on the
    # same characters, which is correct: the author should see both. So this is
    # not the mechanism that keeps a stylistic suggestion from evicting a real
    # correction; `force_query` is. It is last anyway, so that if that invariant
    # ever broke, the arbitration would still give every contested span to the
    # surer source rather than to a matter of taste.
    if on_phase and cfg.smoothing.enabled and prepared.whole_document:
        on_phase("smoothing")
    smoothing_findings, smoothing_report = _smoothing_findings(
        cfg, prepared, usage, out_dir=out)
    # Chapter-scoped continuity rides alongside smoothing, and for the same
    # reasons: it is a whole-book pass whose every finding is force_query'd, and
    # finish() runs exactly once per book. It is handed the anchors the whole-book
    # continuity read already flagged (both are error_type "continuity" — the
    # model read and the calendar tier), so the two passes never ask the author
    # the same question twice.
    book_anchors = frozenset(
        (f.para_id, " ".join(f.original_text.split()))
        for f in model_findings if f.error_type == "continuity")
    if on_phase and cfg.chapter_continuity.enabled and prepared.whole_document:
        on_phase("chapter_continuity")
    chapter_continuity_findings, chapter_continuity_report = \
        _chapter_continuity_findings(cfg, prepared, usage, out_dir=out,
                                     book_anchors=book_anchors,
                                     batch_reads=chapter_batch_reads)
    proposed = (list(prepared.sweep_findings)
                + list(prepared.consistency_findings)
                + sapling_findings
                + model_findings
                + promoted
                + smoothing_findings
                + chapter_continuity_findings)

    def _validate(findings):
        return validate_findings(findings, prepared.doc, cfg.min_confidence,
                                 query_types=prepared.query_types,
                                 format_types=prepared.format_types,
                                 edit_guard=cfg.edit_guard)

    validated = _validate(proposed)
    # The judge gates: the last read before the manuscript is written. They run
    # AFTER validation on purpose — by now the survivors are exactly the changes
    # that would reach the author, so a judge reads each one once, and never one
    # that a later gate would have thrown away anyway.
    #
    # They are deliberately SUBTRACTIVE: a change a judge will not vouch for is
    # turned into a margin question in place (validator.to_query), and nothing
    # else in the run moves. The tempting alternative — set force_query and
    # re-run the validator — is wrong, because a second arbitration re-opens
    # every span: the withdrawn change frees the one it held, an edit that had
    # been set aside as overlapping is promoted into it, and that promoted edit
    # can in turn evict a DIFFERENT change this same gate had just approved.
    # Turning on a safety pass must not delete a correction the safety pass
    # itself vouched for, so the spans stay exactly as the arbitration settled
    # them. Running gates one after another is safe for the same reason: each
    # only ever removes, and a change the first withdrew is no longer
    # "validated", so the second neither sees it nor is billed for it.
    judge_reports, held_count = _run_judge_gates(
        cfg, prepared, validated, usage, out_dir=out, replay=judge_held,
        on_phase=on_phase, coverage=coverage)
    # Residual coverage, after every gate has settled what is actually being
    # edited: number-rule trigger sites no surviving edit touched become
    # margin queries, so a rule the model applied to most-but-not-all matches
    # ends the run accounted for instead of silently partial. Validated
    # separately (they need the settled anchors to know what was touched);
    # queries claim no spans, so the second validation cannot evict anything.
    if cfg.residuals.enabled:
        from .residuals import residual_queries
        covered_ids = {p.para_id for c in prepared.chunks
                       for p in c.paragraphs}
        residual = residual_queries(
            [p for p in prepared.doc.paragraphs if p.para_id in covered_ids],
            validated, max_per_rule=cfg.residuals.max_per_rule)
        validated += _validate(residual)
    # One comment per repeated rule explanation, the rest silenced — after
    # every gate has settled which edits stand, so the count in the surviving
    # comment is the count in the manuscript.
    _collapse_repeated_comments(validated, prepared.doc, cfg.comment_collapse)
    # Every paid pass is behind us — the residual scan and the comment collapse
    # above are free and local — so what's left is assembling the document.
    if on_phase:
        on_phase("writing")
    # Verifier rejections were never candidates for a tracked change, but they
    # belong in the report so the author sees what the overseer set aside.
    validated = validated + verifier_rejected
    fmt = prepared.fmt
    # A note at the top of the file naming the words the spell scan took on
    # trust — placed before the edits, since it only inserts comment range
    # markers and those leave every later offset untouched. Format-optional and
    # gated on Word comments being on at all.
    if (cfg.comments and cfg.excluded_words_comment
            and fmt.annotate_excluded_words and prepared.spell.available
            and prepared.spell.lexicon):
        fmt.annotate_excluded_words(prepared.pkg, prepared.doc,
                                    prepared.spell.lexicon, cfg.revision_author)
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
                        coverage=coverage, smoothing=smoothing_report,
                        chapter_continuity=chapter_continuity_report,
                        # Both reassemblers report these; getattr keeps a format
                        # that predates them constructing rather than crashing.
                        queried_ids=getattr(stats, "queried", ()),
                        unplaced_ids=getattr(stats, "unplaced", ()))
    write_summary_md(out / "summary.md", doc=prepared.doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied,
                     batch=batch, fmt=fmt, sweeps=prepared.sweep_reports,
                     spell=prepared.spell,
                     normalization=prepared.normalization, audit=audit_report,
                     consistency=prepared.consistency, coverage=coverage,
                     judges=judge_reports, smoothing=smoothing_report,
                     chapter_continuity=chapter_continuity_report)
    change_log = None
    if cfg.change_log:
        change_log = out / fmt.change_log_name(source_path)
        write_change_log(change_log, doc=prepared.doc, findings=validated,
                         cfg=cfg, applied_ids=stats.applied,
                         sweeps=prepared.sweep_reports, spell=prepared.spell,
                         normalization=prepared.normalization,
                         audit=audit_report, usage=usage,
                         variant=prepared.variant, fmt=fmt)

    enforce(audit_report, cfg.audit)
    prepared.pkg.save(reviewed)
    # Counted off the same list the reports are written from, not off
    # `stats.queried`: with query_comments on, the reassembler's tally also
    # counts the low-confidence and oversized comments it writes, and misses
    # the queries that found no anchor. This number is the one summary.md
    # prints, and both formats reach it the same way — IDML writes its queries
    # as inline Notes and carries the same `queried`/`unplaced` stats the .docx
    # reassembler does, so the count is a count of what is in the file either
    # way.
    return Outputs(reviewed_path=reviewed, summary_md=out / "summary.md",
                   findings_json=out / "findings.json",
                   applied=len(stats.applied), findings=len(validated),
                   change_log=change_log,
                   queried=sum(1 for f in validated if f.status == "query"),
                   judge_held=held_count,
                   warnings=([f"{d.label}: {d.reason}"
                              for d in coverage.degraded]
                             if coverage is not None else []))
