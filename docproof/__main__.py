from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import logging
import sys
from pathlib import Path

from . import __version__
from . import batch as batchlib
from .analyzer import MockAnalyzer
from .config import load_config
from .ingest import IngestError
from .logging_setup import setup_logging
from .models import CoverageLedger, Usage
from .pipeline import chunk_outline, finish, prepare, run_sync
from .profiles import CANDIDATE_ONLY, DETECTOR_ONLY, PROFILE_KEYS, apply_profile
from .providers import ProviderError, build_provider, estimate_cost
from .variants import VARIANT_KEYS

log = logging.getLogger("docproof.main")

DEFAULT_WORKSPACE = "jobs"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="docproof",
        description="LLM-assisted grammar review with native tracked changes, "
                    "in Word (.docx) and InDesign (.idml) files.")
    ap.add_argument("--version", action="version",
                    version=f"docproof {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory",
                         help="ingest + chunk only (no API): preview a run")
    inv.add_argument("input", help="a .docx or .idml file")
    inv.add_argument("--config", default="config/default.yaml")
    inv.add_argument("--model")
    _profile_arg(inv)

    rev = sub.add_parser("review", help="run the full pipeline now")
    _common(rev)
    rev.add_argument("--max-chunks", type=int,
                     help="review only the first N chunks (cheap smoke test)")
    rev.add_argument("--only", help="review only these sections: "
                                    "comma-separated chunk ids from "
                                    "`docproof inventory` "
                                    "(e.g. chunk-003,chunk-007)")
    rev.add_argument("--mock-findings",
                     help="JSON file of raw findings; skips the API entirely")
    rev.add_argument("--resume", action="store_true",
                     help="if a findings checkpoint from an interrupted run is "
                          "in --out, replay it and skip the paid reads, going "
                          "straight to writing the deliverable. A checkpoint is "
                          "written automatically after the reads finish, so a "
                          "crash while writing never re-buys them.")
    rev.add_argument("--rounds", type=int,
                     help="review this many times, each round reading the "
                          "previous round's corrections, with a strong judge "
                          "ruling on every change between rounds "
                          "(default: config rounds.count)")
    rev.add_argument("--meaning-check", action="store_true",
                     help="before writing, have a strong model read every "
                          "proposed change — the model's, Sapling's, "
                          "LanguageTool's — and hold back any that alters what "
                          "a sentence means (default: config "
                          "meaning_check.enabled)")
    rev.add_argument("--meaning-model",
                     help="which model reads the changes for the meaning check "
                          "(default: config meaning_check.model)")
    rev.add_argument("--fix-check", action="store_true",
                     help="before writing, have a strong model read every "
                          "proposed change and hold back any whose replacement "
                          "is not the right fix (default: config "
                          "fix_check.enabled)")
    rev.add_argument("--fix-model",
                     help="which model reads the changes for the fix check "
                          "(default: config fix_check.model)")

    sub_batch = sub.add_parser(
        "submit", help="queue a review at batch prices (50%% cheaper, "
                       "results within hours)")
    _common(sub_batch)
    sub_batch.add_argument("--max-chunks", type=int)
    sub_batch.add_argument("--only", help="review only these sections: "
                                          "comma-separated chunk ids from "
                                          "`docproof inventory` "
                                          "(e.g. chunk-003,chunk-007)")
    sub_batch.add_argument("--workspace", default=DEFAULT_WORKSPACE)

    st = sub.add_parser("status", help="show queued batch reviews")
    st.add_argument("job_id", nargs="?")
    st.add_argument("--config", default="config/default.yaml")
    st.add_argument("--workspace", default=DEFAULT_WORKSPACE)

    col = sub.add_parser("collect", help="finish a queued review")
    col.add_argument("job_id")
    col.add_argument("--config", default="config/default.yaml")
    col.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    col.add_argument("--out")

    prp = sub.add_parser(
        "prep", help="tag a manuscript into the house InDesign style set")
    prp.add_argument("input", help="a .docx manuscript (.doc/.rtf/.odt/.txt "
                                   "are converted first, if LibreOffice is "
                                   "installed)")
    prp.add_argument("--config", default="config/default.yaml")
    prp.add_argument("--out", help="output directory (default: from config)")
    prp.add_argument("--output", default=None,
                     choices=["book", "indesign", "tracked", "both", "all"],
                     help="which file(s) to write: the book-styled reading "
                          "copy, the InDesign-ready .docx, the tracked-changes "
                          ".docx, 'both' (indesign+tracked, as before books "
                          "existed) or 'all' (default: from config)")
    prp.add_argument("--style-sheet",
                     help="a different house style set (YAML). This is how you "
                          "prep for another template without touching code.")
    prp.add_argument("--subject", default="",
                     help="the book output's subject matter (picks the "
                          "title-page face). Default: detected from the "
                          "opening pages.")
    prp.add_argument("--book-title", default="",
                     help="running-head title for the book output "
                          "(default: detected, else the file name)")
    prp.add_argument("--book-author", default="",
                     help="running-head author for the book output "
                          "(default: detected)")
    prp.add_argument("--model")
    prp.add_argument("--no-verify", action="store_true",
                     help="skip the word-for-word check. Not recommended: it "
                          "is the only thing standing between a mis-tagged "
                          "run and a changed manuscript.")
    prp.add_argument("--mock-tags", action="store_true",
                     help="label everything as running text; exercises the "
                          "writers and the verifier with no API call")

    cmp = sub.add_parser(
        "compare", help="compare the tracked changes on two .docx files "
                        "(e.g. a human proofread vs docproof's output)")
    cmp.add_argument("doc_a", help="the baseline / ground-truth doc "
                                   "(e.g. the human-proofread manuscript)")
    cmp.add_argument("doc_b", help="the doc to compare against it "
                                   "(e.g. docproof's Atmosphere Press "
                                   "Proofreader output)")
    cmp.add_argument("--config", default="config/default.yaml")
    cmp.add_argument("--out", help="where to write the report "
                                   "(default: from config)")
    cmp.add_argument("--label-a", default="human",
                     help="name for doc_a in the report (default: human)")
    cmp.add_argument("--label-b", default="docproof",
                     help="name for doc_b in the report (default: docproof)")
    cmp.add_argument("--formatting", action="store_true",
                     help="compare InDesign paragraph styling instead of "
                          "tracked changes (a human's hand-tagging vs prep's)")

    ev = sub.add_parser(
        "eval", help="score the model against the held-out corpus "
                     "(docs/accuracy-eval-plan.md)")
    ev.add_argument("--config", default="config/default.yaml")
    ev.add_argument("--model")
    ev.add_argument("--error-types",
                    help="score only these passes (same syntax as review); "
                         "cases of other types are skipped")
    ev.add_argument("--out", help="where to write the scorecard "
                                  "(default: from config)")
    ev.add_argument("--cases-dir", default="eval/cases",
                    help="the corpus directory (default: eval/cases)")
    ev.add_argument("--gate", choices=["low", "medium", "high"],
                    default="medium",
                    help="confidence gate the per-type table is shown at; the "
                         "full low/medium/high curve is always reported")
    ev.add_argument("--mock-findings",
                    help="JSON file of raw findings; scores the harness itself "
                         "with no API call")

    rj = sub.add_parser(
        "rejudge", help="run the judge gates over a review that already ran, "
                        "without reviewing it again")
    rj.add_argument("results",
                    help="the output directory of the finished review "
                         "(the one holding findings.json)")
    rj.add_argument("--config", default="config/default.yaml")
    rj.add_argument("--out", help="where to write the re-judged deliverable "
                                  "(default: <results>/rejudged)")
    rj.add_argument("--source",
                    help="the manuscript the review read, if it has moved "
                         "since (default: the path findings.json recorded)")
    rj.add_argument("--meaning-check", action="store_true",
                    help="run the meaning gate (does the corrected sentence "
                         "still mean what the original meant?)")
    rj.add_argument("--meaning-model",
                    help="which model reads the changes for the meaning check")
    rj.add_argument("--fix-check", action="store_true",
                    help="run the fix gate (is the replacement the right fix?)")
    rj.add_argument("--fix-model",
                    help="which model reads the changes for the fix check")

    swp = sub.add_parser(
        "sweep", help="run one agent-authored bespoke fix (a --rule) over a "
                      "manuscript — a book-specific author tic no shipped "
                      "detector catches. Dry-run by default; --apply writes "
                      "tracked changes. No API.")
    swp.add_argument("input", help="a .docx or .idml file")
    swp.add_argument("--rule", required=True,
                     help="the rule file: a .yaml/.json regex rule "
                          "(pattern + replacement), or a .py sweep defining "
                          "SWEEP or a scan() function")
    swp.add_argument("--config", default="config/default.yaml")
    swp.add_argument("--out", help="output directory for --apply "
                                   "(default: from config)")
    swp.add_argument("--apply", action="store_true",
                     help="write the tracked-changes .docx (default is a dry "
                          "run that counts and samples but writes nothing)")
    swp.add_argument("--sample", type=int, default=5,
                     help="how many before/after examples to show (default: 5)")
    swp.add_argument("--variant", choices=list(VARIANT_KEYS),
                     help="which English the manuscript is in (affects "
                          "ingest/normalization; default: config variant)")
    swp.add_argument("--force", action="store_true",
                     help="apply even if the rule is not idempotent — matches "
                          "remain after its own fix — which is almost always a "
                          "bug in the rule")
    swp.add_argument("--json", action="store_true",
                     help="also print the machine-readable result to stdout")

    mrg = sub.add_parser(
        "merge", help="the merge desk: reconcile a mechanical/proofread "
                      "findings set with a copy-edit/rewrite findings set "
                      "into one deliverable with two tracked-changes "
                      "authors. Writes by default; --dry-run only prints "
                      "the claim ledger. No API.")
    mrg.add_argument("input", help="a .docx or .idml file")
    mrg.add_argument("--mechanical", required=True,
                     help="findings JSON for the mechanical/proofread lane "
                          "(a JSON array, or a {'findings': [...]} "
                          "envelope — e.g. a docproof review --resume "
                          "checkpoint's findings, or findings.json). A "
                          "finding with no 'lane' field lands here.")
    mrg.add_argument("--copyedit",
                     help="findings JSON for the copy-edit/rewrite lane, "
                          "same shape (default: none — a mechanical-only "
                          "run, useful to smoke-test the artifact scan "
                          "alone). A finding's own 'lane' field, when "
                          "present, overrides which lane it lands in.")
    mrg.add_argument("--config", default="config/default.yaml")
    mrg.add_argument("--out", help="output directory "
                                   "(default: from config)")
    mrg.add_argument("--dry-run", action="store_true",
                     help="print the claim ledger — who won each contested "
                          "span, and why — and write nothing")
    mrg.add_argument("--variant", choices=list(VARIANT_KEYS),
                     help="which English the manuscript is in (default: "
                          "config variant)")
    mrg.add_argument("--json", action="store_true",
                     help="also print the machine-readable result to stdout")

    _galley_parser(sub)

    args = ap.parse_args(argv)
    return {"inventory": cmd_inventory, "review": cmd_review,
            "submit": cmd_submit, "status": cmd_status,
            "collect": cmd_collect, "prep": cmd_prep, "rejudge": cmd_rejudge,
            "eval": cmd_eval, "compare": cmd_compare, "sweep": cmd_sweep,
            "merge": cmd_merge,
            "galley": cmd_galley}[args.cmd](args)


def _galley_parser(sub) -> None:
    """The `galley` command group: headless entry points for the practitioner
    tools that until now only tests could reach — audit a finished run for what
    it missed, render the editorial letter, and run the reference-free recall
    gauge. These wrap galley/{audit,letter,seeding}.py; only `audit` calls a
    model."""
    gal = sub.add_parser(
        "galley", help="practitioner tools over a finished run: audit for "
                       "missed errors, write the editorial letter, gauge recall")
    gsub = gal.add_subparsers(dest="galley_cmd", required=True)

    ga = gsub.add_parser(
        "audit", help="read a finished run for likely MISSED errors — one model "
                      "call over the quietest chapters, where a miss hides")
    ga.add_argument("results", help="the finished run's output directory "
                                    "(the one holding findings.json)")
    ga.add_argument("--source", required=True,
                    help="the manuscript that run reviewed (.docx or .idml), "
                         "read for the by-chapter density and page samples")
    ga.add_argument("--config", default="config/default.yaml")
    ga.add_argument("--model",
                    help="the auditing model (default: the continuity reader if "
                         "one is configured, else the review model)")
    ga.add_argument("--n-samples", type=int, default=6,
                    help="pages to sample from the quiet chapters (default: 6)")
    ga.add_argument("--out", help="where to write audit.json "
                                  "(default: the results directory)")
    ga.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")

    gl = gsub.add_parser(
        "letter", help="render the editorial letter and style sheet from a case "
                       "file — a report on the run, no API, no new decisions")
    gl.add_argument("casefile", help="a galley casefile.json, or a directory "
                                     "holding one")
    gl.add_argument("--source",
                    help="the manuscript, for per-chapter finding counts in the "
                         "letter (optional; .docx or .idml)")
    gl.add_argument("--config", default="config/default.yaml")
    gl.add_argument("--out", help="directory for letter.md and style-sheet.md "
                                  "(default: beside the case file)")
    gl.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")

    gs = gsub.add_parser(
        "seed", help="plant known, reversible errors into a copy of a few "
                     "chapters — the first move of the reference-free recall "
                     "gauge; no API")
    gs.add_argument("source", help="the manuscript (.docx or .idml)")
    gs.add_argument("-n", "--count", type=int, default=8,
                    help="how many errors to plant (default: 8)")
    gs.add_argument("--seed", type=int, default=0,
                    help="RNG seed; the same seed replays the same plants "
                         "byte-for-byte (default: 0)")
    gs.add_argument("--config", default="config/default.yaml")
    gs.add_argument("--out", required=True,
                    help="directory for seeded_manuscript.json and "
                         "answer_key.json")
    gs.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")

    gc = gsub.add_parser(
        "score", help="grade a fleet's findings against a seed answer key — the "
                      "last move of the recall gauge; no API")
    gc.add_argument("findings", help="a JSON array of galley findings (or a "
                                     "{'findings': [...]} envelope)")
    gc.add_argument("--answer-key", required=True,
                    help="the answer_key.json written by `galley seed`")
    gc.add_argument("--out", help="where to write recall.json "
                                  "(default: beside the findings file)")
    gc.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("input", help="a .docx or .idml file")
    p.add_argument("--config", default="config/default.yaml")
    p.add_argument("--out", help="output directory (default: from config)")
    p.add_argument("--error-types",
                   help="override enabled types: comma-separated passes, "
                        "'+' to combine types into one pass "
                        "(e.g. spelling+repeated_word,comma_splice)")
    p.add_argument("--model")
    p.add_argument("--min-confidence", choices=["low", "medium", "high"])
    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--variant", choices=list(VARIANT_KEYS),
                   help="which English this manuscript is written in: it flips "
                        "the conventions that differ by variant (dialogue mark, "
                        "percent vs per cent, dates, that/which) and picks the "
                        "spell-scan dictionary (default: config variant)")
    p.add_argument("--dictionary",
                   help="path to a Hunspell .aff/.dic pair, without the "
                        "extension, for the spell scan. Only needed when the "
                        "variant's dictionary is not bundled — spylls ships "
                        "en_US alone (default: config spellcheck.dictionary)")
    _profile_arg(p)


def _profile_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--profile", choices=list(PROFILE_KEYS),
        help="apply a reproducible review profile; 'detector-only' runs one "
             "low-effort production detector pass with Phase 2 receipts and "
             "writes tracked changes only; 'candidate-only' runs the explicit "
             "candidate detector alone through the guarded tracked-change path")


def _selection(args) -> list[str] | None:
    raw = getattr(args, "only", None)
    if not raw:
        return None
    return [c.strip() for c in raw.split(",") if c.strip()]


def _configure(args):
    cfg = load_config(args.config)
    if getattr(args, "error_types", None):
        cfg.error_types = [[k.strip() for k in group.split("+") if k.strip()]
                           for group in args.error_types.split(",")
                           if group.strip()]
    if getattr(args, "min_confidence", None):
        cfg.min_confidence = args.min_confidence
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if getattr(args, "dictionary", None):
        cfg.spellcheck.dictionary = args.dictionary
    if getattr(args, "no_comments", False):
        cfg.comments = False
    if getattr(args, "out", None):
        cfg.output_dir = args.out
    # A profile is a strict boundary, so it lands after general config knobs.
    # The reviewer model is the one deliberate exception: experiments may hold
    # the profile constant while comparing detectors with --model.
    apply_profile(cfg, getattr(args, "profile", None))
    if getattr(args, "model", None):
        cfg.api.model = args.model
    error_dir = Path(args.config).parent / "error_types"
    return cfg, error_dir


def cmd_inventory(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        prepared = prepare(cfg, args.input, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    doc_tokens = sum(c.est_tokens for c in prepared.chunks)
    plan = prepared.effective_pass_plan
    print(f"{len(prepared.doc.paragraphs)} reviewable paragraphs → "
          f"{len(prepared.chunks)} chunks (~{doc_tokens:,} document tokens)")
    print(f"{len(cfg.error_type_keys)} error type(s) in "
          f"{len(cfg.error_type_groups)} category/-ies, {len(plan)} pass(es) → "
          f"{prepared.request_count} API calls, "
          f"~{prepared.est_document_tokens:,} document tokens sent")
    for spec in cfg.error_type_specs:
        extra = []
        if spec.passes > 1:
            extra.append(f"x{spec.passes}")
        if spec.token_budget:
            extra.append(f"@{spec.token_budget}t")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        print(f"  category: {' + '.join(spec.keys)}{suffix}")

    swept = sum(r.flagged for r in prepared.sweep_reports)
    if prepared.sweep_reports:
        print(f"\n{len(prepared.sweep_reports)} scripted sweep(s) → "
              f"{swept} correction(s), already found, no API call:")
        for r in prepared.sweep_reports:
            print(f"  {r.key:<28} {r.flagged:>4} flagged, "
                  f"{r.remaining} remaining")

    # Output tokens are unknowable up front; assume a modest cap per request so
    # the number is an order-of-magnitude guide, not a quote.
    out_guess = prepared.request_count * 600
    now = estimate_cost(cfg.api.model, input_tokens=prepared.est_document_tokens,
                        output_tokens=out_guess)
    if now is not None:
        print(f"\nRough cost on {cfg.api.model}: ~${now:.2f} now, "
              f"~${now / 2:.2f} as a batch (50% cheaper, results within hours)")

    # The continuity read is one whole-book pass on its own model, added on top of
    # the review above — priced once at the document's own token count, not the
    # per-chunk total, and not batchable (a different model can't ride the review
    # batch). The deterministic calendar check is free. Over max_input_tokens the
    # pipeline skips the read, so the estimate says so rather than quoting a cost
    # for a call that won't be made.
    if cfg.continuity.enabled:
        if doc_tokens > cfg.continuity.max_input_tokens:
            print(f"  + continuity: book exceeds max_input_tokens "
                  f"({cfg.continuity.max_input_tokens:,}); the read is skipped, "
                  f"only the free calendar check runs")
        else:
            cont = estimate_cost(cfg.continuity.model, input_tokens=doc_tokens,
                                 output_tokens=cfg.continuity.max_output_tokens)
            if cont is not None:
                print(f"  + continuity read on {cfg.continuity.model}: "
                      f"~${cont:.2f} (one whole-book read, query-only, "
                      f"not batchable)")

    # Chapter continuity reads the book once, split across chapters, plus a small
    # judge — priced at the document's tokens in, and each chapter's output
    # ceiling out, so the estimate scales with how the book segments.
    if cfg.chapter_continuity.enabled:
        from .continuity import chapters as _chapters
        cc = cfg.chapter_continuity
        cc_model = cc.model or cfg.continuity.model or cfg.api.model
        units = _chapters(prepared.doc.paragraphs, cfg.skip.is_sweep_only,
                          min_tokens=cc.min_chapter_tokens,
                          max_tokens=cc.max_chapter_tokens)
        chap = estimate_cost(cc_model, input_tokens=doc_tokens,
                             output_tokens=len(units) * cc.max_output_tokens)
        if chap is not None:
            print(f"  + chapter continuity on {cc_model}: ~${chap:.2f} "
                  f"({len(units)} chapter read(s), query-only, plus a small "
                  f"judge on {cc.judge_model}); the reads ride their own batch, "
                  f"so a batch submission roughly halves this")

    print("\nSections (pass any of these to --only):")
    for row in chunk_outline(prepared):
        print(f"  {row['chunk_id']:<12} {row['paragraphs']:>3} para "
              f"~{row['est_tokens']:>5,}tok  {row['preview'][:60]}")
    for pid, reason in prepared.doc.skipped:
        print(f"  skipped {pid:<24} {reason}")
    return 0


def cmd_review(args) -> int:
    cfg, error_dir = _configure(args)
    out = Path(cfg.output_dir)
    setup_logging(out)

    profile = getattr(args, "profile", None)
    if profile in (DETECTOR_ONLY, CANDIDATE_ONLY):
        incompatible = []
        if args.rounds not in (None, 1):
            incompatible.append("--rounds")
        if args.meaning_check or args.meaning_model:
            incompatible.append("--meaning-check/--meaning-model")
        if args.fix_check or args.fix_model:
            incompatible.append("--fix-check/--fix-model")
        if incompatible:
            print(f"error: {profile} cannot be combined with "
                  + ", ".join(incompatible), file=sys.stderr)
            return 2
    if args.rounds is not None:
        cfg.rounds.count = args.rounds
    # The gate is a flag rather than a tri-state: absent leaves whatever the
    # config says, so a house config that ships it on is not silently disarmed
    # by every CLI run. Naming a model implies wanting the gate.
    if getattr(args, "meaning_model", None):
        cfg.meaning_check.model = args.meaning_model
        cfg.meaning_check.enabled = True
    if getattr(args, "meaning_check", False):
        cfg.meaning_check.enabled = True
    if getattr(args, "fix_model", None):
        cfg.fix_check.model = args.fix_model
        cfg.fix_check.enabled = True
    if getattr(args, "fix_check", False):
        cfg.fix_check.enabled = True
    if cfg.rounds.count > 1:                          # multi-round review
        from .rounds import run_sync_rounds
        canned = None
        if args.mock_findings:
            canned = _load_mocks(args.mock_findings)
            if canned is None:
                return 2
        try:
            outputs = run_sync_rounds(cfg, args.input, error_dir, out_dir=out,
                                      source_path=args.input,
                                      mock_findings=canned)
        except (IngestError, FileNotFoundError, ValueError, ProviderError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"\n{_result_line(outputs)}.")
        for p in (outputs.reviewed_path, outputs.change_log,
                  outputs.summary_md, outputs.findings_json, out / "run.log"):
            if p is not None:
                print(f"  {p}")
        return 0

    # prepare() is deterministic and API-free, so it runs before any provider is
    # built: its content hash fingerprints the checkpoint, and a --resume needs
    # the Prepared to hand to finish() regardless.
    try:
        prepared = prepare(cfg, args.input, error_dir,
                           max_chunks=args.max_chunks,
                           selection=_selection(args))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    from . import run_checkpoint
    fingerprint = {"content_hash": prepared.content_hash,
                   "model": cfg.api.model,
                   "config": str(Path(args.config).resolve())}

    coverage = None
    resumed = False
    if args.resume and not args.mock_findings:
        hit = run_checkpoint.load(out, fingerprint=fingerprint)
        if hit is not None:
            findings, usage, coverage = hit.findings, hit.usage, hit.coverage
            resumed = True
            print(f"Resumed from checkpoint: {len(findings)} finding(s) "
                  f"replayed, paid reads skipped.")
        else:
            print("No usable checkpoint in the output directory; "
                  "reviewing fresh.")

    if not resumed:
        if args.mock_findings:
            canned = _load_mocks(args.mock_findings)
            if canned is None:
                return 2
            findings, usage = _run_mock(cfg, prepared, canned)
        else:
            try:
                provider = build_provider(cfg)
            except ProviderError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            coverage = CoverageLedger()
            findings, usage = run_sync(cfg, prepared, provider, coverage=coverage)
            # The paid reads are done. Snapshot them before finish()'s late
            # stages so a crash there never buys the detector passes again.
            run_checkpoint.save(out, findings=findings, usage=usage,
                                coverage=coverage, fingerprint=fingerprint)

    outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                     source_path=args.input, coverage=coverage)
    # finish() wrote the deliverable and findings.json; the checkpoint has done
    # its job and must not shadow a later, different run.
    run_checkpoint.clear(out)
    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    return 0


def cmd_submit(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        provider = build_provider(cfg)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        job = batchlib.submit(cfg, args.input, error_dir, provider,
                              args.workspace, max_chunks=args.max_chunks,
                              selection=_selection(args))
    except (IngestError, batchlib.BatchError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Queued {job.source_name} as job {job.job_id}")
    print(f"  {job.request_count} request(s) at batch prices on {job.model}")
    print(f"  Results usually arrive within a few hours (24h at the outside).")
    print(f"\nCheck on it:  docproof status {job.job_id}")
    print(f"Finish it:    docproof collect {job.job_id}")
    return 0


def cmd_status(args) -> int:
    cfg, _ = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        jobs = ([batchlib.load(args.workspace, args.job_id)] if args.job_id
                else batchlib.load_all(args.workspace))
    except (batchlib.BatchError, json.JSONDecodeError, TypeError) as e:
        # load_all shrugs off unreadable manifests; asking for one by id has
        # to answer in words too, not a traceback.
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not jobs:
        print(f"No queued reviews in {args.workspace}/")
        return 0

    provider = None
    for job in jobs:
        if job.active:
            if provider is None:
                try:
                    provider = build_provider(cfg)
                except ProviderError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 2
            try:
                batchlib.poll(job, provider, args.workspace)
            except Exception as e:            # noqa: BLE001 - report, continue
                log.warning("Could not refresh %s: %s", job.job_id, e)
        print(f"{job.job_id:<40} {_plain_state(job):<28} {job.source_name}")
        if job.error:
            print(f"    {job.error}")
    return 0


def cmd_collect(args) -> int:
    cfg, error_dir = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        job = batchlib.load(args.workspace, args.job_id)
        provider = build_provider(cfg)
        outputs = batchlib.collect(job, provider, error_dir, args.workspace,
                                   out_dir=args.out)
    except (batchlib.BatchError, ProviderError, IngestError, ValueError,
            TypeError) as e:
        # ValueError is the edited-document case: the manifest names chunks
        # the re-chunked source no longer has, which the content-hash check
        # never gets to see. (It also covers a corrupt manifest's JSON.)
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json):
        if p is not None:
            print(f"  {p}")
    return 0


def cmd_rejudge(args) -> int:
    """Put a finished review's corrections to the judge gates, and nothing else.

    Its own command rather than a flag on `review` because it is a different
    job: `review` finds corrections, this one rules on corrections already
    found. It makes no detector call and re-reads nothing — the record the
    original run left is the input, so a book proofread before these gates
    existed can be gated now for the price of the gates alone."""
    from .rejudge import RejudgeError, rejudge

    cfg = load_config(args.config)
    error_dir = Path(args.config).parent / "error_types"
    if args.meaning_model:
        cfg.meaning_check.model = args.meaning_model
        cfg.meaning_check.enabled = True
    if args.meaning_check:
        cfg.meaning_check.enabled = True
    if args.fix_model:
        cfg.fix_check.model = args.fix_model
        cfg.fix_check.enabled = True
    if args.fix_check:
        cfg.fix_check.enabled = True

    out = Path(args.out) if args.out else Path(args.results, "rejudged")
    setup_logging(out)
    usage = Usage()
    try:
        outputs = rejudge(cfg, args.results, out_dir=out, error_dir=error_dir,
                          source=args.source, usage=usage)
    except (RejudgeError, ProviderError, IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)} ({usage.api_calls} judge call(s)).")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json):
        if p is not None:
            print(f"  {p}")
    return 0


def cmd_prep(args) -> int:
    """Tag a manuscript for the house template.

    Deliberately its own command rather than a flag on `review`: prep answers
    what each paragraph IS, review answers what is wrong inside one, and a run
    that tried to do both would have to choose which of two very different
    documents to hand back."""
    from . import prep as preplib
    from .prep.convert import ConversionError, ensure_docx

    cfg = load_config(args.config)
    if args.model:
        cfg.api.model = args.model
    if args.out:
        cfg.output_dir = args.out
    if args.style_sheet:
        cfg.prep.style_sheet = args.style_sheet
    if args.no_verify:
        cfg.prep.verify = False
    kinds = (["indesign", "tracked"] if args.output == "both"
             else list(preplib.OUTPUT_KINDS) if args.output == "all"
             else [args.output] if args.output else list(cfg.prep.outputs))

    out = Path(cfg.output_dir)
    setup_logging(out)
    config_dir = Path(args.config).parent

    try:
        source, note = ensure_docx(args.input, out)
        prepared = preplib.prepare(cfg, source, config_dir=config_dir)
    except (ConversionError, IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if note:
        print(f"note: {note}")

    print(f"{prepared.paragraph_count} paragraphs → {prepared.request_count} "
          f"request(s) on {cfg.api.model}, ~"
          f"{prepared.est_document_tokens:,} tokens sent")

    provider = None
    if args.mock_tags:
        tags, usage = preplib.run_mock(prepared)
    else:
        try:
            provider = build_provider(cfg)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        tags, usage = preplib.run(
            cfg, prepared, provider,
            progress=lambda done, total: print(
                f"  labelled window {done} of {total}", flush=True))

    meta = None
    if "book" in kinds:
        detected = (preplib.detect_meta(cfg, prepared, provider, usage=usage)
                    if provider is not None else preplib.BookMeta())
        meta = preplib.merge_meta(detected, subject=args.subject,
                                  title=args.book_title,
                                  author=args.book_author)
        print(f"  book sketch: subject '{meta.subject or 'default'}', "
              f"title {meta.title or Path(args.input).stem!r}, "
              f"author {meta.author or '(none)'}")

    try:
        outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                 source_path=args.input, outputs=kinds,
                                 meta=meta)
    except preplib.VerificationFailed as e:
        print(f"error: {e}", file=sys.stderr)
        print(f"  see {out / 'prep_notes.md'} for what prep intended to do.",
              file=sys.stderr)
        return 3

    print(f"\n{outputs.tagged} paragraph(s) tagged, "
          f"{outputs.plan.inserted_breaks} scene break(s) written, "
          f"{outputs.flags} flag(s) for the designer.")
    for check in outputs.verifications:
        print(f"  ✓ {check.describe()}")
    for path in list(outputs.documents.values()) + [outputs.notes_md,
                                                    outputs.notes_json]:
        print(f"  {path}")
    return 0


def cmd_compare(args) -> int:
    import json as _json

    from .trackdiff import (TrackDiffError, compare_edits, extract_edits,
                            open_docx, render_markdown, report_json)

    cfg = load_config(args.config)
    if getattr(args, "out", None):
        cfg.output_dir = args.out
    out = Path(cfg.output_dir)
    setup_logging(out)

    if getattr(args, "formatting", False):
        return _cmd_compare_formatting(args, out)

    try:
        a = extract_edits(open_docx(args.doc_a))
        b = extract_edits(open_docx(args.doc_b))
    except TrackDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    report = compare_edits(a, b, label_a=args.label_a, label_b=args.label_b)

    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "compare.md"
    json_path = out / "compare.json"
    md_path.write_text(render_markdown(report, a.base), encoding="utf-8")
    json_path.write_text(
        _json.dumps(report_json(report, a.base), indent=2, ensure_ascii=False),
        encoding="utf-8")

    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"{args.label_a}: {a.edit_count} edit(s)  ·  "
          f"{args.label_b}: {b.edit_count} edit(s)  ·  "
          f"{report.aligned_paras} paragraph(s) compared")
    if report.unaligned:
        print(f"  {len(report.unaligned)} paragraph(s) skipped "
              f"(base text differs — not comparable)")
    print(f"\n  agree {report.agree}  ·  different fix {report.diff_fix}  ·  "
          f"only {args.label_a} {report.only_a}  ·  "
          f"only {args.label_b} {report.only_b}")
    if report.query_located:
        print(f"  ({report.query_located} of the only-{args.label_a} were "
              f"flagged by a {args.label_b} query)")
    print(f"\nScored against {args.label_a} as ground truth:")
    print(f"  located recall {pct(report.located_recall)}  ·  "
          f"exact recall {pct(report.exact_recall)}  ·  "
          f"precision {pct(report.precision)}  ·  F1 {pct(report.f1)}")
    if report.query_located:
        print(f"  located recall + queries "
              f"{pct(report.located_recall_with_queries)} "
              f"(crediting a query that points at the right span)")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


def _cmd_compare_formatting(args, out: Path) -> int:
    """`compare --formatting`: diff the InDesign paragraph styling on two docs
    instead of their tracked changes."""
    import json as _json

    from . import formatdiff

    try:
        a = formatdiff.extract_styles(formatdiff.open_docx(args.doc_a))
        b = formatdiff.extract_styles(formatdiff.open_docx(args.doc_b))
    except formatdiff.TrackDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    report = formatdiff.compare_styles(a, b, label_a=args.label_a,
                                       label_b=args.label_b)

    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "compare-formatting.md"
    json_path = out / "compare-formatting.json"
    md_path.write_text(formatdiff.render_markdown(report), encoding="utf-8")
    json_path.write_text(
        _json.dumps(formatdiff.report_json(report), indent=2,
                    ensure_ascii=False),
        encoding="utf-8")

    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"{args.label_a}: {a.styled_count} paragraph(s)  ·  "
          f"{args.label_b}: {b.styled_count} paragraph(s)  ·  "
          f"{report.aligned_paras} compared")
    print(f"\n  same style {report.agree}  ·  different {report.different}  ·  "
          f"only {args.label_a} {len(report.only_a)}  ·  "
          f"only {args.label_b} {len(report.only_b)}")
    print(f"\n  style agreement {pct(report.agreement)}")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


def cmd_eval(args) -> int:
    from .error_registry import shipped_keys
    from .eval.corpus import CorpusError, check_no_leakage, load_corpus
    from .eval.runner import run_eval
    from .eval.scorecard import write_scorecard
    from .eval.scorer import family, score_curve
    from .labels import DID_NOT_RUN, will_produce

    cfg, error_dir = _configure(args)
    out = Path(cfg.output_dir)
    setup_logging(out)

    try:
        cases = load_corpus(args.cases_dir)
        check_no_leakage(cases, error_dir)
    except (CorpusError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # The corpus is per-type; scoring only makes sense for the types the run
    # actually exercises. --error-types narrows the passes; narrow the cases to
    # match so a skipped type is not scored as all-misses.
    #
    # Asked of the whole FAMILY, and through `will_produce` rather than through
    # `cfg.error_type_keys`. A case is caught by any label in its family, and
    # the aliases are free-form labels that the error-type list cannot contain
    # under any config — so keying the filter off that list alone dropped every
    # repeated_word case from a `--error-types spelling` run even though
    # sweep_doubled_word was still running and still catching them, and scored
    # the sweeps' whole contribution as out of scope. A case whose family is
    # entirely unproducible is genuinely unscoreable; anything less certain is
    # kept, because an over-inclusive corpus reports as a visible miss and an
    # under-inclusive one reports as nothing at all.
    known = shipped_keys(error_dir)
    kept, dropped = [], []
    for case in cases:
        producible = any(
            will_produce(cfg, label, known_types=known) != DID_NOT_RUN
            for label in family(case.error_type))
        (kept if producible else dropped).append(case)
    if dropped:
        print(f"Not scored — no pass in this run produces them: "
              f"{', '.join(sorted({c.error_type for c in dropped}))} "
              f"({len(dropped)} case(s))")
    cases = kept
    if not cases:
        print("error: no corpus cases match the passes this run performs.",
              file=sys.stderr)
        return 2

    provider = None
    mock = None
    if args.mock_findings:
        mock = _load_mocks(args.mock_findings)
        if mock is None:
            return 2
    else:
        try:
            provider = build_provider(cfg)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    n_err = sum(1 for c in cases if not c.is_clean)
    n_trap = len(cases) - n_err
    print(f"Scoring {len(cases)} cases ({n_err} seeded errors, {n_trap} traps) "
          f"across {len({c.error_type for c in cases})} type(s) on "
          f"{cfg.api.model}{' [mock]' if mock else ''}")

    def progress(done, total):
        print(f"  reviewing {done}/{total}", end="\r", flush=True)

    work = out / "eval"
    try:
        run = run_eval(cfg, cases, error_dir, work, provider=provider,
                       mock_findings=mock,
                       progress=None if mock else progress)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    curve = score_curve(run)
    json_path, md_path = write_scorecard(curve, work, default_gate=args.gate)

    card = curve[args.gate]
    p, r, f = card.micro
    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    print(f"\nAt the {args.gate} gate: "
          f"precision {pct(p)}, recall {pct(r)}, F1 {pct(f)}")
    print(f"  trap false-positive rate: {pct(card.trap_fp_rate)}")
    print(f"  anchor-failure rate:      {pct(card.anchor_failure_rate)}")
    if card.cost is not None:
        print(f"  cost: ${card.cost:.2f}")
    print(f"\n  {md_path}\n  {json_path}")
    return 0


# --- bespoke sweep ------------------------------------------------------------

def cmd_sweep(args) -> int:
    """Run one bespoke, book-scoped sweep over a manuscript — the $0
    deterministic front door for an author tic no shipped detector anticipates
    (doubled dialogue quotes, a home-made scene break). Dry-run by default:
    count, sample, and check idempotency, writing nothing. `--apply` writes the
    tracked-changes .docx, refusing a non-idempotent rule unless `--force`."""
    from .custom_sweep import RuleError, load_rule
    from .sweeps import run_sweep_objects

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if args.out:
        cfg.output_dir = args.out
    # A bespoke run is ONLY the rule. Silence every other free analyzer and
    # every finish-stage model pass so nothing else edits, queries, or bills —
    # the run stays deterministic and needs no provider.
    cfg.sweeps = []
    cfg.style.unclosed_quote_queries = False
    cfg.style.heading_title_case = False
    cfg.consistency.enabled = False
    cfg.spellcheck.enabled = False
    cfg.meaning_check.enabled = False
    cfg.fix_check.enabled = False
    cfg.repair.enabled = False
    cfg.smoothing.enabled = False
    cfg.continuity.enabled = False
    cfg.chapter_continuity.enabled = False
    cfg.rounds.count = 1

    try:
        sweep = load_rule(args.rule)
    except RuleError as e:
        print(f"error: --rule {args.rule}: {e}", file=sys.stderr)
        return 2

    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = Path(args.config).parent / "error_types"
    try:
        prepared = prepare(cfg, args.input, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        findings, reports = run_sweep_objects(
            prepared.doc.paragraphs, [sweep], prepared.variant,
            ellipsis_style=cfg.style.ellipsis)
    except Exception as e:                # noqa: BLE001 - a rule's scan is user code
        print(f"error: rule '{sweep.key}' failed while scanning: {e}",
              file=sys.stderr)
        return 2
    report = reports[0]
    idempotent = report.remaining == 0

    print(f"\nBespoke sweep '{report.key}' — {report.name}")
    print(f"  {report.flagged} match(es) across "
          f"{len(prepared.doc.paragraphs)} paragraph(s).")
    if not idempotent:
        print(f"  ⚠ not idempotent: {report.remaining} match(es) remain after "
              f"the fix — the rule would fire again on its own output.")
    shown = findings[:max(0, args.sample)]
    for f in shown:
        print(f"    {f.para_id}: {f.original_text!r} → {f.corrected_text!r}")
    if len(findings) > len(shown):
        print(f"    … and {len(findings) - len(shown)} more")

    result = {"key": report.key, "name": report.name,
              "flagged": report.flagged, "remaining": report.remaining,
              "idempotent": idempotent, "paragraphs": len(prepared.doc.paragraphs),
              "applied": False}

    if not args.apply:
        print("\n  Dry run — nothing written. Re-run with --apply to write "
              "tracked changes.")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0

    if report.flagged == 0:
        print("\n  Nothing to apply.")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    if not idempotent and not args.force:
        print(f"\nerror: refusing to apply a non-idempotent rule "
              f"({report.remaining} match(es) remain after its own fix). Fix "
              f"the rule, or pass --force to apply it anyway.", file=sys.stderr)
        return 3

    # The bespoke findings ride the sweep channel finish() already applies; zero
    # the other prepared channels so ONLY this rule lands.
    prepared.sweep_findings = findings
    prepared.sweep_reports = reports
    prepared.consistency_findings = []
    usage = Usage()
    try:
        outputs = finish(prepared, [], usage, cfg, out_dir=out,
                         source_path=args.input)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    result["applied"] = True
    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        result["reviewed_path"] = (str(outputs.reviewed_path)
                                   if outputs.reviewed_path else None)
        print(json.dumps(result, ensure_ascii=False))
    return 0


# --- merge desk -----------------------------------------------------------------

def _load_findings_file(path: Path) -> list[dict] | None:
    """A findings JSON file: a bare array, or a `{"findings": [...]}` envelope
    (what `findings.json` and a run checkpoint both write). None on any
    problem, after printing why."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"error: could not read {path}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"error: {path}: {e}", file=sys.stderr)
        return None
    rows = raw.get("findings", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        print(f"error: {path}: must be a JSON array of finding objects, or a "
              "{'findings': [...]} envelope", file=sys.stderr)
        return None
    return rows


_LEDGER_VERBS = {"rewrite_clean": "copy-edit rewrite (clean) subsumes",
                 "mechanical_default": "mechanical wins over",
                 "cluster_atomic": "cluster claims its span for"}


def cmd_merge(args) -> int:
    """The merge desk: reconcile a mechanical/proofread findings set with a
    copy-edit/rewrite findings set into one deliverable with two tracked-
    changes authors (docproof/mergedesk.py). $0, like `docproof sweep`: every
    paid pass is silenced, and the claim rules run over the deterministic
    sweeps + LanguageTool, never a provider. Writes by default; `--dry-run`
    only prints the claim ledger."""
    from . import mergedesk

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    if args.out:
        cfg.output_dir = args.out
    # Only the two lanes' findings are reconciled here — no new paid pass,
    # model read, or judge gate runs. The built-in sweeps are deliberately
    # LEFT ON (unlike `docproof sweep`, which zeroes them): they are what
    # gives a house-style span (an ellipsis, a stacked-punctuation run)
    # priority over either lane's edit, exactly as in an ordinary review.
    cfg.consistency.enabled = False
    cfg.spellcheck.enabled = False
    cfg.meaning_check.enabled = False
    cfg.fix_check.enabled = False
    cfg.repair.enabled = False
    cfg.smoothing.enabled = False
    cfg.continuity.enabled = False
    cfg.chapter_continuity.enabled = False
    cfg.low_confidence.confirm = False
    cfg.rounds.count = 1

    mech_raw = _load_findings_file(Path(args.mechanical))
    if mech_raw is None:
        return 2
    ce_raw: list[dict] = []
    if args.copyedit:
        ce_raw = _load_findings_file(Path(args.copyedit))
        if ce_raw is None:
            return 2

    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = Path(args.config).parent / "error_types"
    try:
        prepared = prepare(cfg, args.input, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        mechanical = mergedesk.tag_lane(mech_raw, mergedesk.MECHANICAL)
        copyedit = mergedesk.tag_lane(ce_raw, mergedesk.COPYEDIT)
    except mergedesk.MergeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    merged = mergedesk.merge_lanes(
        mechanical, copyedit, prepared.doc, min_confidence=cfg.min_confidence,
        query_types=prepared.query_types, format_types=prepared.format_types,
        edit_guard=cfg.edit_guard, variant=prepared.variant,
        ellipsis_style=cfg.style.ellipsis)
    merged, artifacts = mergedesk.iterate_until_clean(
        merged, prepared.doc, min_confidence=cfg.min_confidence,
        query_types=prepared.query_types, format_types=prepared.format_types,
        edit_guard=cfg.edit_guard)

    print(f"\nMerge desk: {len(mechanical)} mechanical + {len(copyedit)} "
          f"copy-edit finding(s) → {len(merged.ledger)} contested span(s).")
    for r in merged.ledger:
        tail = f" — {r.reason}" if r.reason else ""
        loser = f" {r.loser_id}" if r.loser_id else ""
        print(f"  {r.para_id} [{r.start}:{r.end}]  {_LEDGER_VERBS[r.rule]}"
              f"{loser}{tail}  (winner {r.winner_id}, {r.winner_lane})")
    if artifacts:
        print(f"\n{len(artifacts)} merged-result artifact(s):")
        for hit in artifacts:
            state = f"dropped {hit.dropped_id}" if hit.resolved else "UNRESOLVED"
            print(f"  {hit.para_id or '?'}: {hit.pattern} — {state}")
    unresolved = [h for h in artifacts if not h.resolved]

    result = {
        "findings": [dataclasses.asdict(f) for f in merged.validated],
        "cost": {"total": 0.0},
        "ledger": {"claims": [r.to_json() for r in merged.ledger]},
        "claims": {"contested": len(merged.ledger),
                  "artifacts": [h.to_json() for h in artifacts]},
        "checkpoint": None,
    }

    if args.dry_run:
        print("\n  Dry run — nothing written. Re-run without --dry-run to "
              "write the deliverable.")
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 3 if unresolved else 0

    if unresolved:
        print("\nerror: unresolved merged-result artifact(s) — refusing to "
              "write. Re-run with --dry-run to inspect them, or fix the "
              "finding(s) that contribute to them.", file=sys.stderr)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 3

    usage = Usage()
    try:
        outputs = finish(prepared, merged.findings, usage, cfg, out_dir=out,
                         source_path=args.input)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if outputs.findings_json is not None:
        try:
            written = json.loads(outputs.findings_json.read_text(encoding="utf-8"))
            result["findings"] = written.get("findings", written) \
                if isinstance(written, dict) else written
        except (OSError, json.JSONDecodeError):
            pass
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 0


# --- galley -------------------------------------------------------------------

def cmd_galley(args) -> int:
    """Dispatch the `galley` sub-verbs. Kept apart from the review commands: these
    read a finished run rather than producing one."""
    return {"audit": _galley_audit, "letter": _galley_letter,
            "seed": _galley_seed, "score": _galley_score}[args.galley_cmd](args)


def _galley_audit(args) -> int:
    from galley.audit import audit_run, chapter_densities, read_findings
    from galley.ingest import manuscript_from_source
    from .providers import cost_of_usage

    cfg = load_config(args.config)
    results = Path(args.results)
    if not (results / "findings.json").exists():
        print(f"error: no findings.json in {results} — point the results "
              f"argument at a finished run's output directory", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else results
    setup_logging(out)

    # The audit is a whole-book reasoning read, so it wants the strong reader the
    # continuity pass uses, not the cheap per-chunk detector model.
    model = args.model or cfg.continuity.model or cfg.api.model
    try:
        provider = build_provider(cfg)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    usage = Usage()
    hyps = audit_run(results, ms, provider, model, usage,
                     n_samples=args.n_samples)
    densities = chapter_densities(read_findings(results), ms)
    cost = cost_of_usage(usage, fallback_model=model) or 0.0

    payload = {
        "results_dir": str(results),
        "source": args.source,
        "model": model,
        "n_samples": args.n_samples,
        "hypotheses": [h.to_json() for h in hyps],
        "densities": [d.to_json() for d in densities],
        "cost_usd": round(cost, 4),
        "api_calls": usage.api_calls,
    }
    out.mkdir(parents=True, exist_ok=True)
    audit_path = out / "audit.json"
    audit_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    print(f"\n{len(hyps)} hypothesis/-es about likely missed errors "
          f"({usage.api_calls} model call(s), ${cost:.2f}).")
    quiet = sorted(densities, key=lambda d: (d.per_1k, d.index))[:5]
    if quiet:
        print("  quietest chapters (a miss most likely hides here):")
        for d in quiet:
            print(f"    ch {d.index:>3}  {d.per_1k:>5.2f}/1k  "
                  f"{d.findings:>4} finding(s) in {d.words:>6} words  "
                  f"{d.title[:40]}")
    for h in hyps[:10]:
        why = h.why[:70] + ("…" if len(h.why) > 70 else "")
        print(f"  - ch {h.chapter}: {h.error_class} ({h.confidence}) — {why}")
    print(f"\n  {audit_path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def _galley_letter(args) -> int:
    from galley.casefile import CaseFile
    from galley.letter import render_all

    target = Path(args.casefile)
    cf_path = target / "casefile.json" if target.is_dir() else target
    if not cf_path.exists():
        print(f"error: {cf_path} not found — pass a casefile.json or a "
              f"directory holding one", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else cf_path.parent
    setup_logging(out)
    try:
        cf = CaseFile.load(cf_path)
    except (OSError, ValueError) as e:
        print(f"error: could not read {cf_path}: {e}", file=sys.stderr)
        return 2

    ms = None
    if args.source:
        from galley.ingest import manuscript_from_source
        cfg = load_config(args.config)
        try:
            ms = manuscript_from_source(args.source, cfg)
        except (IngestError, FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    letter_path, style_path = render_all(cf, out, ms=ms)
    open_queries = sum(1 for v in cf.verdicts if v.ruling == "query")
    print(f"\nEditorial letter for {cf.book or '(untitled)'}: "
          f"{len(cf.findings)} finding(s), {len(cf.waves)} wave(s), "
          f"{open_queries} open query/-ies, {_money(cf.budget.spent_usd)} spent.")
    print(f"  {letter_path}\n  {style_path}")
    if args.json:
        print(json.dumps({
            "book": cf.book,
            "letter": str(letter_path),
            "style_sheet": str(style_path),
            "findings": len(cf.findings),
            "waves": len(cf.waves),
            "open_queries": open_queries,
            "spent_usd": round(cf.budget.spent_usd, 4),
        }, ensure_ascii=False))
    return 0


def _galley_seed(args) -> int:
    from galley.ingest import manuscript_from_source
    from galley.seeding import seed_copy

    cfg = load_config(args.config)
    out = Path(args.out)
    setup_logging(out)
    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    seeded, key = seed_copy(ms, args.count, rng_seed=args.seed)

    out.mkdir(parents=True, exist_ok=True)
    seeded_path = out / "seeded_manuscript.json"
    key_path = out / "answer_key.json"
    seeded_path.write_text(
        json.dumps(seeded.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    key_path.write_text(
        json.dumps(key.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    by_type: dict[str, int] = {}
    for pe in key.planted:
        by_type[pe.error_type] = by_type.get(pe.error_type, 0) + 1
    print(f"\nPlanted {len(key.planted)} of {key.requested} requested error(s) "
          f"across chapter(s) {', '.join(map(str, key.seeded_chapters)) or '—'} "
          f"(seed {key.rng_seed}).")
    for et in sorted(by_type):
        print(f"  {et:<22} {by_type[et]}")
    if len(key.planted) < key.requested:
        print("  (fewer than requested — the sampled chapters ran out of "
              "mutable sites)")
    print(f"\n  {seeded_path}\n  {key_path}")
    print("  Next: run the fleet on the seeded text, then `galley score` its "
          "findings against the answer key.")
    if args.json:
        print(json.dumps({
            "seeded_manuscript": str(seeded_path),
            "answer_key": str(key_path),
            "planted": len(key.planted),
            "requested": key.requested,
            "seeded_chapters": list(key.seeded_chapters),
            "rng_seed": key.rng_seed,
        }, ensure_ascii=False))
    return 0


def _galley_score(args) -> int:
    from galley.contracts import GFinding
    from galley.seeding import AnswerKey, score_catches

    findings_path = Path(args.findings)
    key_path = Path(args.answer_key)
    try:
        raw = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {findings_path}: {e}", file=sys.stderr)
        return 2
    rows = raw.get("findings", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        print("error: findings must be a JSON array (or a {'findings': [...]} "
              "envelope)", file=sys.stderr)
        return 2
    findings = [GFinding.from_json(r) for r in rows if isinstance(r, dict)]

    try:
        key = AnswerKey.from_json(json.loads(key_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {key_path}: {e}", file=sys.stderr)
        return 2

    est = score_catches(findings, key)
    out = Path(args.out) if args.out else findings_path.parent
    out.mkdir(parents=True, exist_ok=True)
    recall_path = out / "recall.json"
    payload = {
        "planted": est.planted,
        "caught": est.caught,
        "rate": round(est.rate, 4),
        "by_type": {k: list(v) for k, v in est.by_type.items()},
        "caveat": est.caveat,
    }
    recall_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    print(f"\n{est.summary()}")
    for et in sorted(est.by_type):
        caught, planted = est.by_type[et]
        print(f"  {et:<22} {caught}/{planted}")
    print(f"\n  {est.caveat}")
    print(f"\n  {recall_path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


# --- helpers ------------------------------------------------------------------

def _money(value: float) -> str:
    return f"${value:,.2f}"


def _result_line(outputs) -> str:
    """What a finished review produced, both halves of it — no full stop, so a
    caller can add its own tail.

    A review hands back tracked changes and margin questions, and printing only
    the first tells someone who ran it on the command line that a book with
    forty questions waiting in it needed nothing looked at."""
    line = f"{outputs.applied} tracked change(s) applied"
    if outputs.queried:
        line += f", {outputs.queried} question(s) left in the margins"
    return line


def _plain_state(job) -> str:
    return {"submitted": "waiting on the provider",
            "in_progress": "being reviewed",
            "ready": "ready to collect",
            "done": "finished",
            "failed": "needs attention"}.get(job.state, job.state)


def _load_mocks(path: str):
    try:
        canned = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: --mock-findings {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(canned, list):
        print("error: --mock-findings must be a JSON array of findings.",
              file=sys.stderr)
        return None
    return canned


def _run_mock(cfg, prepared, canned):
    ids = itertools.count(1)
    usage = Usage()
    findings = []
    for prun in prepared.effective_pass_plan:
        analyzer = MockAnalyzer(list(prun.types), canned, ids)
        for chunk in prun.chunks:
            found, _ = analyzer.analyze_chunk(chunk, usage)
            findings.extend(found)
    # Mock mode makes no candidate judge calls, but deterministic candidate
    # errors still exercise the same production bridge and downstream guards.
    if (prepared.candidate_screening is not None
            and cfg.candidate_screening.mode == "apply"):
        findings.extend(prepared.candidate_screening.production_findings())
    return findings, usage


if __name__ == "__main__":
    sys.exit(main())
