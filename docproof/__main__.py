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
    _stage_arg(inv)
    _genre_arg(inv)
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
    rev.add_argument("--json", action="store_true",
                     help="also print the machine-readable result envelope "
                          "to stdout (see docproof/contract.py)")

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

    imf = sub.add_parser(
        "import-findings",
        help="turn a findings file produced outside docproof (a subagent "
             "flight, a hand-built JSON file) into a $0 tracked-changes "
             "deliverable — the front door for injecting external findings "
             "into a build. No API.")
    imf.add_argument("findings", help="a findings JSON file: the contract "
                                      "envelope, a findings.json report, or a "
                                      "bare JSON array of finding dicts "
                                      "(para_id, original_text, "
                                      "corrected_text, and optionally "
                                      "occurrence/confidence/explanation/"
                                      "error_type)")
    imf.add_argument("manuscript", help="the .docx or .idml manuscript these "
                                        "findings were produced against")
    imf.add_argument("--config", default="config/default.yaml")
    imf.add_argument("--out", help="output directory (default: from config)")
    imf.add_argument("--dry-run", action="store_true",
                     help="anchor and channel every row and report what would "
                          "happen; write nothing")
    imf.add_argument("--json", action="store_true",
                     help="also print the machine-readable result envelope "
                          "to stdout")

    rpl = sub.add_parser(
        "replay",
        help="rebuild a deliverable at $0 from an existing findings "
             "checkpoint (run_checkpoint.py's findings.checkpoint.json) or a "
             "finished run's findings.json. No API.")
    rpl.add_argument("findings", help="findings.checkpoint.json, "
                                      "findings.json, or any file holding a "
                                      "'findings' array in that shape")
    rpl.add_argument("manuscript", help="the .docx or .idml manuscript to "
                                        "rebuild against (normally the same "
                                        "one the original run reviewed)")
    rpl.add_argument("--config", default="config/default.yaml")
    rpl.add_argument("--out", help="output directory (default: from config)")
    rpl.add_argument("--dry-run", action="store_true",
                     help="anchor and channel every row and report what would "
                          "happen; write nothing")
    rpl.add_argument("--json", action="store_true",
                     help="also print the machine-readable result envelope "
                          "to stdout")

    _galley_parser(sub)

    args = ap.parse_args(argv)
    return {"inventory": cmd_inventory, "review": cmd_review,
            "submit": cmd_submit, "status": cmd_status,
            "collect": cmd_collect, "prep": cmd_prep, "rejudge": cmd_rejudge,
            "eval": cmd_eval, "compare": cmd_compare, "sweep": cmd_sweep,
            "merge": cmd_merge,
            "import-findings": cmd_import_findings, "replay": cmd_replay,
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

    gq = gsub.add_parser(
        "ask", help="email the press a question only a person can answer — "
                    "Galley's escalation push channel, over the shared DocWatch "
                    "notify address; loud when it cannot send")
    gq.add_argument("subject", help="one line: what the question is about")
    gq.add_argument("--body", help="the question itself (or use --file/stdin): "
                                   "what you were doing, the question, your "
                                   "recommended answer, what is blocked")
    gq.add_argument("--file", dest="body_file",
                    help="read the body from a file (e.g. QUESTIONS.md)")
    gq.add_argument("--book", default="",
                    help="the book this concerns, named in the subject tag")

    gcal = gsub.add_parser(
        "calibrate", help="close the self-measurement loop: seed known errors, "
                          "run the $0 detector floor over them, score recall, "
                          "and record cost/recall calibration the planner can "
                          "read live — no API by default")
    gcal.add_argument("source", nargs="?",
                      help="the manuscript (.docx or .idml); required unless "
                           "--from-run is given")
    gcal.add_argument("-n", "--count", type=int, default=8,
                      help="how many errors to plant (default: 8)")
    gcal.add_argument("--seed", type=int, default=0,
                      help="RNG seed; the same seed replays the same plants "
                           "(default: 0)")
    gcal.add_argument("--config", default="config/default.yaml")
    gcal.add_argument("--book", default="",
                      help="book name recorded with this calibration sample "
                           "(default: the source filename)")
    gcal.add_argument("--calibration",
                      help="the calibration JSON store to read and update "
                           "(default: galley_calibration.json beside --config)")
    gcal.add_argument("--out",
                      help="directory for seeded_manuscript.json/answer_key.json "
                           "(default: the current directory)")
    gcal.add_argument("--from-run", metavar="RESULTS_DIR",
                      help="skip seeding and scoring; instead record cost "
                           "calibration from an existing casefile.json in this "
                           "directory (source is still required, to resolve "
                           "word counts for that run's scopes)")
    gcal.add_argument("--model",
                      help="not yet wired into the seeded closed loop — the "
                           "paid adapters re-read the source from disk, not "
                           "the in-memory seeded copy, so recall can't be "
                           "scored for a paid pass yet. Calibrate a real run's "
                           "cost instead with --from-run")
    gcal.add_argument("--json", action="store_true",
                      help="also print the machine-readable result to stdout")

    glp = gsub.add_parser(
        "profile", help="a deterministic ($0) profile of a manuscript: word "
                        "count, chapter structure, dialogue density, "
                        "proper-noun candidates, repeated author tics, "
                        "reading-level metrics, and a genre guess")
    glp.add_argument("input", help="a .docx or .idml file")
    glp.add_argument("--config", default="config/default.yaml")
    glp.add_argument("--json", action="store_true",
                     help="print the profile as JSON instead of a summary")
    glp.add_argument("--out", help="write the profile JSON to this path "
                                   "(in addition to any --json/summary print)")
    glp.add_argument("--model",
                     help="spend one call on this model to confirm the "
                          "genre guess and add curated notes (default: no "
                          "call, $0). Best-effort: any failure keeps the "
                          "deterministic profile unchanged.")

    glg = gsub.add_parser(
        "genre-pack", help="materialize a run config: base config + a genre "
                           "posture preset + (optionally) profile-derived "
                           "name/era seeding")
    glg.add_argument("genre", choices=list(_genre_choices()),
                     help="which posture preset to apply")
    glg.add_argument("--base", default="config/default.yaml",
                     help="the base run config (default: config/default.yaml)")
    glg.add_argument("--out", required=True,
                     help="where to write the materialized run config")
    glg.add_argument("--profile",
                     help="a profile JSON from `docproof galley profile "
                          "--json --out`; its proper-noun candidates seed "
                          "consistency.seeded_names / spellcheck.allowlist")
    glg.add_argument("--stage", choices=list(_stage_choices()) or None,
                     help="also apply a workflow-stage preset (which lanes "
                          "run), composed BEFORE the genre and with its lane "
                          "locks re-asserted after, so the genre cannot reopen "
                          "a lane the stage forbids. See config/stages/")
    glg.add_argument("--era", type=int,
                     help="the manuscript's setting, as a year AD the "
                          "vocabulary should predate — turns the anachronism "
                          "scan from a no-op into an active scan (historical "
                          "genre). Never inferred; you state it.")

    gf = gsub.add_parser(
        "flights", help="the copy-edit lane: a panel of narrow taste passes "
                        "(lenses), unioned by overlapping span, ruled on by "
                        "one skeptical judge. Runs on ALREADY-PROOFREAD text — "
                        "this lane never runs on raw manuscript; the merge "
                        "desk owns combining its output with a proofread.")
    gf.add_argument("input", nargs="?",
                    help="a .docx or .idml file, already proofread. Omit only "
                         "with --judge-only, which reads an existing clusters "
                         "file instead of the manuscript")
    gf.add_argument("--config", default="config/default.yaml")
    gf.add_argument(
        "--models", default="gpt-5.6-luna",
        help="comma-separated proposer model ids; each reads every one of "
             "--lenses, so N models x M lenses = N*M flights (default: "
             "gpt-5.6-luna alone — measured 42%% taste-recall on 6 lenses; "
             "adding a second model, e.g. claude-sonnet-5, measured 55-57%%)")
    gf.add_argument(
        "--lenses", default="economy,word_choice,flow,clarity,rhythm,repetition",
        help="comma-separated lenses to run (default: all 6). 'general' is "
             "also available — an undecomposed baseline for an A/B, not part "
             "of the default matrix")
    gf.add_argument("--judge-model", default="claude-fable-5",
                    help="the judge — one call per cluster, ~90%% of the "
                         "lane's cost (default: claude-fable-5)")
    gf.add_argument(
        "--posture", choices=["strict", "lenient"], default=None,
        help="strict defaults to keeping the original (measured ~24%% of "
             "proposals accepted); lenient leans toward accepting (~57%%). "
             "Same proposals, same hard vetoes (voice/meaning/fragment/"
             "lateral-swap) either way — only the default and how generously "
             "'defensible' reads moves. Genre tailoring sets this per "
             "manuscript (default: strict)")
    gf.add_argument("--min-confidence", choices=["low", "medium", "high"],
                    default="medium",
                    help="accept floor on the judge's own confidence "
                         "(default: medium)")
    gf.add_argument("--propose-max-tokens", type=int, default=8000)
    gf.add_argument("--judge-max-tokens", type=int, default=1200)
    gf.add_argument("--concurrency", type=int, default=4,
                    help="propose windows / judge calls in flight at once "
                         "(default: 4)")
    gf.add_argument("--out", help="output directory (default: from config)")
    gf.add_argument(
        "--propose-only", action="store_true",
        help="propose and cluster, then stop — write clusters.json instead "
             "of spending on the judge. Lets an external flight (a session "
             "subagent, a human) rule on the clusters later via --judge-only")
    gf.add_argument(
        "--judge-only", metavar="CLUSTERS_JSON",
        help="skip propose/cluster; judge an existing clusters.json (from "
             "--propose-only) and write findings. No manuscript needed — a "
             "cluster carries its own paragraph text")
    gf.add_argument(
        "--external-proposals", metavar="PROPOSALS_JSON",
        help="a JSON array (or {'proposals': [...]}) of additional proposals "
             "— para_id, quote (or original), replacement, rationale, and "
             "optionally model/lens — merged in before clustering, through "
             "the same deterministic filters as an API flight. For a session "
             "subagent acting as a flight without an API call of its own")
    gf.add_argument("--dry-run", action="store_true",
                    help="project cost from the manuscript's word count; no "
                         "API calls, no keys")
    gf.add_argument("--variant", choices=list(VARIANT_KEYS))
    gf.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")


def _genre_choices() -> tuple[str, ...]:
    from .genre import available_genres
    return available_genres()


def _stage_choices() -> tuple[str, ...]:
    from .stages import available_stages
    return available_stages()


def _stage_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--stage", choices=list(_stage_choices()) or None,
        help="apply a workflow-stage preset (which lanes run) before the "
             "genre posture, and re-assert its lane LOCKS after it, so a genre "
             "can never turn on a lane the stage forbids (e.g. mechanical-wave "
             "keeps edits-mode smoothing and the rewrite lever off whatever "
             "genre is set). See config/stages/ and docproof/stages.py.")


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
    _stage_arg(p)
    _genre_arg(p)
    _profile_arg(p)


def _profile_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--profile", choices=list(PROFILE_KEYS),
        help="apply a reproducible review profile; 'detector-only' runs one "
             "low-effort production detector pass with Phase 2 receipts and "
             "writes tracked changes only; 'candidate-only' runs the explicit "
             "candidate detector alone through the guarded tracked-change path")


def _genre_arg(p: argparse.ArgumentParser) -> None:
    from .genre import available_genres
    p.add_argument(
        "--genre", choices=list(available_genres()) or None,
        help="apply a genre posture preset to the stylistic lane (smoothing/"
             "rewrite aggressiveness, the flights lane's judge posture, "
             "consistency name-spelling seeding) before any --profile is "
             "applied, so a profile's stricter boundary always wins over a "
             "genre's looser one. See config/genres/ and docproof/genre.py. "
             "For a full genre PACK (this preset plus continuity prompts, "
             "genre-only scans, and profile-derived name/era seeding "
             "materialized into a reviewable run config), use "
             "`docproof galley genre-pack` instead.")


def _selection(args) -> list[str] | None:
    raw = getattr(args, "only", None)
    if not raw:
        return None
    return [c.strip() for c in raw.split(",") if c.strip()]


def _resolve_error_dir(config_path: str | Path) -> Path:
    """Locate the ``error_types/`` prompt directory for a run config.

    Prefer the directory beside the config — ``config/error_types`` when running
    the shipped ``config/default.yaml``, or a workspace's own edited copy. When a
    RELOCATED config has no sibling ``error_types/`` (the common case for a
    materialized genre-pack config written into a book workspace), fall back to
    the packaged shipped directory, so a materialized config is self-contained
    wherever it lives. Before this fallback, such a config failed ``inventory``
    (and every typed pass) by looking for ``error_types/`` beside itself and
    finding nothing. When neither exists, return the beside-path unchanged so the
    downstream FileNotFoundError still names the location the user expected."""
    beside = Path(config_path).parent / "error_types"
    if beside.is_dir():
        return beside
    packaged = Path(__file__).resolve().parent.parent / "config" / "error_types"
    if packaged.is_dir():
        return packaged
    return beside


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
    # A genre posture preset is itself a general config knob — it lands here,
    # before the profile. A profile is a strict boundary, so it lands after
    # every general config knob (genre included): apply_profile turns the
    # whole stylistic lane off in detector-only/candidate-only mode, and a
    # genre applied after it would silently break that boundary's own
    # promise (no comment channel). See docproof/genre.py's module docstring.
    # The reviewer model is the one deliberate exception: experiments may hold
    # the profile constant while comparing detectors with --model.
    # A workflow-stage preset lands BEFORE the genre so its lane locks can be
    # re-asserted AFTER the genre (stage > genre): a genre never turns on a lane
    # the stage forbids. Profile lands last of all (profile > stage > genre).
    stage_locks: dict = {}
    if getattr(args, "stage", None):
        from .stages import apply_stage
        cfg, stage_locks = apply_stage(cfg, args.stage)
    genre_pending = {}
    if getattr(args, "genre", None):
        from .genre import apply_genre
        cfg, genre_pending = apply_genre(cfg, args.genre)
        for key in genre_pending:
            log.warning("genre %r set %s, which this build cannot apply yet "
                       "(no matching config section) — ignored", args.genre,
                       key)
    if stage_locks:
        from .stages import enforce_locks
        violated = enforce_locks(cfg, stage_locks)
        for key in violated:
            log.warning("stage %r locks %s; the genre %r tried to change it — "
                        "the stage lock wins", args.stage, key,
                        getattr(args, "genre", None))
    apply_profile(cfg, getattr(args, "profile", None))
    if getattr(args, "model", None):
        cfg.api.model = args.model
    error_dir = _resolve_error_dir(args.config)
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
        if args.json:
            from .contract import build_envelope
            payload = json.loads(outputs.findings_json.read_text("utf-8"))
            envelope = build_envelope(
                findings=payload.get("findings", []),
                usage=_usage_from_payload(payload), fallback_model=cfg.api.model,
                # Multi-round review runs its own per-round driver rather than
                # this module's run_sync/checkpoint path, so neither a
                # CoverageLedger nor a resumable checkpoint is available here
                # to report — both simply read as empty/null.
                coverage=None, checkpoint=None)
            print(json.dumps(envelope, ensure_ascii=False))
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
    from .batch import pass_prompts
    from .checkpoint import Checkpoint
    # One fingerprint, shared by both checkpoint mechanisms below: the
    # findings-boundary one (run_checkpoint, survives a crash AFTER run_sync)
    # and the per-call one (Checkpoint, survives a crash DURING it — see
    # app/jobs.py's _checkpoint for the pattern this mirrors). Hashing the
    # config's full contents rather than its path is deliberate: a stale
    # fingerprint that only named the path would replay findings from BEFORE
    # an edit to that file, which is a correctness bug, not a cost one.
    fingerprint = {"kind": "review", "content_hash": prepared.content_hash,
                   "config": cfg.model_dump(mode="json"),
                   "prompts": pass_prompts(cfg, prepared),
                   "selection": _selection(args)}

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

    call_checkpoint_path = out / "checkpoint.json"
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
            # The intra-run checkpoint: each completed (pass, chunk) call lands
            # here as it arrives, so a crash mid run_sync — not just after it,
            # which is all the findings-boundary checkpoint below covers — is
            # resumable too. No separate flag gates this the way --resume gates
            # the findings-boundary replay: presence plus a matching
            # fingerprint is what makes it apply (the same rule Checkpoint.load
            # already enforces), so a fresh run's file simply is not there, and
            # `docproof review` picks an interrupted run back up on its own,
            # exactly as the app path does.
            call_checkpoint = Checkpoint(call_checkpoint_path,
                                         fingerprint=fingerprint)
            call_checkpoint.load()
            findings, usage = run_sync(cfg, prepared, provider,
                                       coverage=coverage,
                                       checkpoint=call_checkpoint)
            # The paid reads are done. Snapshot them before finish()'s late
            # stages so a crash there never buys the detector passes again.
            run_checkpoint.save(out, findings=findings, usage=usage,
                                coverage=coverage, fingerprint=fingerprint)

    outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                     source_path=args.input, coverage=coverage)
    # finish() wrote the deliverable and findings.json; both checkpoints have
    # done their job and must not shadow a later, different run.
    run_checkpoint.clear(out)
    call_checkpoint_path.unlink(missing_ok=True)
    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        from .contract import build_envelope
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        # The checkpoint field is almost always null here: both checkpoints
        # were just deleted above, since the run they made resumable finished.
        # A caller sees a path there chiefly when reading an interrupted job's
        # directory rather than a completed --json print.
        envelope = build_envelope(findings=payload.get("findings", []),
                                  usage=usage, fallback_model=cfg.api.model,
                                  coverage=coverage, checkpoint=None)
        print(json.dumps(envelope, ensure_ascii=False))
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
    error_dir = _resolve_error_dir(args.config)
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
    error_dir = _resolve_error_dir(args.config)
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
            print(json.dumps(_envelope(findings=findings, usage=Usage(),
                                       model=cfg.api.model, extra=result),
                             ensure_ascii=False))
        return 0

    if report.flagged == 0:
        print("\n  Nothing to apply.")
        if args.json:
            print(json.dumps(_envelope(findings=findings, usage=Usage(),
                                       model=cfg.api.model, extra=result),
                             ensure_ascii=False))
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
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        print(json.dumps(_envelope(findings=payload.get("findings", []),
                                   usage=usage, model=cfg.api.model,
                                   extra=result), ensure_ascii=False))
    return 0


# --- import-findings / replay --------------------------------------------------

def cmd_import_findings(args) -> int:
    """`docproof import-findings`: the front door for injecting a findings file
    produced OUTSIDE docproof (a subagent flight, a hand-built JSON file) into a
    $0 tracked-changes deliverable. See docproof/replay.py for the machinery
    and, in particular, why every row's error_type is forced onto an
    edit-channel type rather than trusted."""
    return _import_or_replay(args, remap_unchanneled=True, id_prefix="import")


def cmd_replay(args) -> int:
    """`docproof replay`: rebuild a deliverable at $0 from a findings checkpoint
    or a finished run's findings.json — docproof's OWN prior output, so unlike
    import-findings, each row's error_type is trusted and left alone. See
    docproof/replay.py."""
    return _import_or_replay(args, remap_unchanneled=False, id_prefix="replay")


def _import_or_replay(args, *, remap_unchanneled: bool, id_prefix: str) -> int:
    from .replay import (DEFAULT_IMPORT_TYPE, WordCountDelta, build_findings,
                        load_findings_file, word_count_delta_guard,
                        zero_paid_passes)
    from .validator import validate_findings

    cfg = load_config(args.config)
    if args.out:
        cfg.output_dir = args.out
    # Zeroed BEFORE prepare(): storysheet and candidate_screening are the two
    # stages prepare() itself can spend on, and both must be off no matter what
    # the loaded config says — see zero_paid_passes' docstring.
    zero_paid_passes(cfg)
    out = Path(cfg.output_dir)
    setup_logging(out)
    error_dir = _resolve_error_dir(args.config)

    try:
        rows = load_findings_file(args.findings)
    except (OSError, ValueError) as e:
        print(f"error: {args.findings}: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"error: {args.findings}: not valid JSON ({e})", file=sys.stderr)
        return 2

    try:
        prepared = prepare(cfg, args.manuscript, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    findings, rejects, remapped = build_findings(
        rows, variant=prepared.variant, error_dir=error_dir,
        remap_unchanneled=remap_unchanneled, id_prefix=id_prefix)

    checked = validate_findings(findings, prepared.doc, cfg.min_confidence,
                                query_types=prepared.query_types,
                                format_types=prepared.format_types)
    tally: dict[str, int] = {}
    for f in checked:
        tally[f.status] = tally.get(f.status, 0) + 1

    verb = "import-findings" if remap_unchanneled else "replay"
    print(f"\n`docproof {verb}`: {len(rows)} row(s) read — {len(findings)} "
          f"usable, {len(rejects)} malformed"
          + (f", {remapped} remapped onto '{DEFAULT_IMPORT_TYPE}' (no reliable "
             f"channel on the row)" if remap_unchanneled else "") + ".")
    for status in sorted(tally):
        print(f"  {status:<24} {tally[status]}")
    if rejects:
        print("\n  malformed row(s):")
        for r in rejects[:10]:
            print(f"    [{r['index']}] {r['reason']}")
        if len(rejects) > 10:
            print(f"    … and {len(rejects) - 10} more")

    extra = {"rows": len(rows), "usable": len(findings),
             "malformed": rejects, "remapped": remapped, "tally": tally}

    if args.dry_run:
        print("\n  Dry run — nothing written. Re-run without --dry-run to "
              "write the deliverable.")
        if args.json:
            print(json.dumps(_envelope(findings=checked, usage=Usage(),
                                       model=cfg.api.model, extra=extra),
                             ensure_ascii=False))
        return 0

    usage = Usage()
    try:
        outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                         source_path=args.manuscript)
    except (IngestError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # A formatting row that reached the change channel deletes the sentence it
    # should only have marked, and the reject-all audit cannot see it. Catch it
    # by word count before calling the deliverable done.
    try:
        word_count_delta_guard(outputs.reviewed_path)
    except WordCountDelta as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"\n{_result_line(outputs)}.")
    for p in (outputs.reviewed_path, outputs.change_log, outputs.summary_md,
              outputs.findings_json, out / "run.log"):
        if p is not None:
            print(f"  {p}")
    if args.json:
        payload = json.loads(outputs.findings_json.read_text("utf-8"))
        print(json.dumps(_envelope(findings=payload.get("findings", []),
                                   usage=usage, model=cfg.api.model,
                                   extra=extra), ensure_ascii=False))
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
    error_dir = _resolve_error_dir(args.config)
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
            "seed": _galley_seed, "score": _galley_score,
            "ask": _galley_ask,
            "calibrate": _galley_calibrate,
            "flights": _galley_flights,
            "profile": cmd_galley_profile,
            "genre-pack": cmd_galley_genre_pack}[args.galley_cmd](args)


def _galley_ask(args) -> int:
    """Push one escalation question over the shared DocWatch Gmail pipe.

    Loud by design: a question that cannot send exits 2 with the fix, so the
    practitioner KNOWS to fall back — log it in QUESTIONS.md and stop the
    blocked thread — instead of waiting on a reply that can never come."""
    import sys

    from app.watch.notify import send_question
    from app.watch.settings import default_watch_home

    if args.body:
        body = args.body
    elif args.body_file:
        body = Path(args.body_file).read_text("utf-8")
    else:
        body = sys.stdin.read()
    if not body.strip():
        print("galley ask: an empty question helps nobody — say what you were "
              "doing, the question, your recommended answer, and what is "
              "blocked.", file=sys.stderr)
        return 2
    try:
        to = send_question(default_watch_home(), args.subject, body,
                           book=args.book)
    except ValueError as e:
        print(f"galley ask: could not send — {e}\nFall back: append the "
              f"question to QUESTIONS.md and stop the blocked work.",
              file=sys.stderr)
        return 2
    except Exception as e:                    # noqa: BLE001 - Gmail refusals land here
        print(f"galley ask: the send failed ({e}).\nFall back: append the "
              f"question to QUESTIONS.md and stop the blocked work.",
              file=sys.stderr)
        return 2
    print(f"Question sent to {to}.")
    return 0


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
        # audit finds HYPOTHESES about missed errors, not Findings — the
        # envelope's `findings` array stays empty; the real payload rides
        # `extra`. `cost` is real: this is the one galley verb that spends.
        print(json.dumps(_envelope(findings=(), usage=usage, model=model,
                                   extra=payload), ensure_ascii=False))
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
        # No API call here — render_all is a report over decisions the case
        # file already recorded — so cost is zero. `cf.findings` are galley
        # GFindings, a different shape than the envelope's Finding-shaped
        # `findings`; the count rides `extra` instead of the array itself.
        print(json.dumps(_envelope(findings=(), usage=Usage(),
                                   model=cfg.api.model if args.source else "",
                                   extra={
                                       "book": cf.book,
                                       "letter": str(letter_path),
                                       "style_sheet": str(style_path),
                                       "findings": len(cf.findings),
                                       "waves": len(cf.waves),
                                       "open_queries": open_queries,
                                       "spent_usd": round(cf.budget.spent_usd, 4),
                                   }), ensure_ascii=False))
    return 0


def _galley_seed(args) -> int:
    from galley.ingest import manuscript_from_source, write_manuscript_docx
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

    # Closes the seed -> review -> score loop: without a real document, the
    # only thing to hand a review was this JSON, which nothing reviews. A
    # .docx source materializes one directly; other formats (.idml) still get
    # the JSON alone until write_manuscript_docx grows a second writer.
    seeded_docx: Path | None = None
    docx_error: str | None = None
    if Path(args.source).suffix.lower() == ".docx":
        try:
            seeded_docx = write_manuscript_docx(
                args.source, cfg, seeded, out / "seeded_manuscript.docx")
        except (IngestError, FileNotFoundError, ValueError) as e:
            docx_error = str(e)
    else:
        docx_error = (f"{Path(args.source).suffix or '(no extension)'} source: "
                      f"only .docx can be materialized into a reviewable "
                      f"document today")

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
    if seeded_docx is not None:
        print(f"  {seeded_docx}")
        print(f"  Next: `docproof review {seeded_docx}`, then `galley score` "
              f"its findings.json against {key_path.name}.")
    else:
        print(f"  (no reviewable document written: {docx_error})")
    if args.json:
        print(json.dumps(_envelope(findings=(), usage=Usage(),
                                   model=cfg.api.model, extra={
                                       "seeded_manuscript": str(seeded_path),
                                       "seeded_docx": (str(seeded_docx)
                                                       if seeded_docx else None),
                                       "answer_key": str(key_path),
                                       "planted": len(key.planted),
                                       "requested": key.requested,
                                       "seeded_chapters": list(key.seeded_chapters),
                                       "rng_seed": key.rng_seed,
                                   }), ensure_ascii=False))
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
        # `findings` here are galley GFindings — a different shape than the
        # envelope's Finding-shaped `findings` array — and no API call was
        # made, so cost is zero; the recall payload rides `extra`.
        print(json.dumps(_envelope(findings=(), usage=Usage(), model="",
                                   extra=payload), ensure_ascii=False))
    return 0


def _galley_calibrate(args) -> int:
    from galley.calibration import (
        DEFAULT_CALIBRATION_FILENAME,
        calibrate_free,
        record_run,
        record_recall,
    )
    from galley.casefile import CaseFile
    from galley.ingest import manuscript_from_source

    if not args.source:
        print("error: source is required (the manuscript to seed, or — with "
              "--from-run — the manuscript that run reviewed)", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    calibration_path = (
        Path(args.calibration) if args.calibration
        else Path(args.config).parent / DEFAULT_CALIBRATION_FILENAME
    )
    calibration_path.parent.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else Path.cwd()
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(out)

    try:
        ms = manuscript_from_source(args.source, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    book = args.book or Path(args.source).stem

    if args.from_run:
        results = Path(args.from_run)
        cf_path = results / "casefile.json" if results.is_dir() else results
        if not cf_path.exists():
            print(f"error: {cf_path} not found — pass a casefile.json or a "
                  f"directory holding one", file=sys.stderr)
            return 2
        try:
            cf = CaseFile.load(cf_path)
        except (OSError, ValueError) as e:
            print(f"error: could not read {cf_path}: {e}", file=sys.stderr)
            return 2

        calibration = record_run(cf, ms, calibration_path)
        entries = sorted(calibration.cost.values(),
                         key=lambda e: (e.adapter, e.model))
        print(f"\nRecorded cost calibration from {cf_path} "
              f"({len(cf.waves)} wave(s)) into {calibration_path}:")
        for e in entries:
            print(f"  {e.adapter:<14} {e.model or '(no model)':<24} "
                  f"${e.usd_per_kword:.4f}/kword  ({e.samples} sample(s), "
                  f"{e.kwords_total:.1f}k word(s) total)")
        if args.json:
            print(json.dumps({
                "calibration": str(calibration_path),
                "cost": {k: e.to_json() for k, e in calibration.cost.items()},
            }, ensure_ascii=False))
        return 0

    if args.model:
        print("error: --model is not yet wired into the seeded closed loop — "
              "the paid adapters re-read the source from disk, not the "
              "in-memory seeded copy, so recall can't be scored for a paid "
              "pass yet. Calibrate a real run's cost instead with --from-run",
              file=sys.stderr)
        return 2

    result = calibrate_free(ms, args.count, rng_seed=args.seed, book=book)
    est = result.estimate

    seeded_path = out / "seeded_manuscript.json"
    key_path = out / "answer_key.json"
    seeded_path.write_text(
        json.dumps(result.seeded.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    key_path.write_text(
        json.dumps(result.answer_key.to_json(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    record_run(result.casefile, result.seeded, calibration_path)
    record_recall(est, calibration_path, book=book)

    print(f"\n{est.summary()}")
    for et in sorted(est.by_type):
        caught, planted = est.by_type[et]
        print(f"  {et:<22} {caught}/{planted}")
    print(f"\n  {est.caveat}")
    print(f"\n  {seeded_path}\n  {key_path}\n  {calibration_path}")
    if args.json:
        print(json.dumps({
            "book": book,
            "calibration": str(calibration_path),
            "seeded_manuscript": str(seeded_path),
            "answer_key": str(key_path),
            "planted": est.planted,
            "caught": est.caught,
            "rate": round(est.rate, 4),
            "by_type": {k: list(v) for k, v in est.by_type.items()},
        }, ensure_ascii=False))
    return 0


def _flights_provider_of(cfg):
    """A `model id -> Provider` resolver for the flights matrix, which can mix
    vendors (`--models gpt-5.6-luna,claude-sonnet-5`). `build_provider(cfg)`
    picks one vendor from `cfg.api`; this instead resolves each model id's own
    vendor (`providers.provider_for`, falling back to `cfg.api.provider` for a
    model the catalog has never heard of) and caches one provider per vendor,
    so a mixed matrix still costs at most three constructions, not one per
    flight."""
    from .providers import ProviderError, provider_for

    cache: dict[str, object] = {}

    def get(model_id: str):
        name = provider_for(model_id, cfg.api.provider)
        if name not in cache:
            kwargs = {"max_retries": cfg.api.max_retries,
                      "prompt_caching": cfg.api.prompt_caching,
                      "effort": cfg.api.effort}
            if name == "anthropic":
                from .providers.anthropic_provider import AnthropicProvider
                cache[name] = AnthropicProvider(**kwargs)
            elif name == "openai":
                from .providers.openai_provider import OpenAIProvider
                cache[name] = OpenAIProvider(**kwargs)
            elif name == "gemini":
                from .providers.gemini_provider import GeminiProvider
                cache[name] = GeminiProvider(**kwargs)
            else:
                raise ProviderError(
                    f"Unknown provider {name!r} for model {model_id!r}. Set "
                    f"api.provider, or use a model id the catalog knows.")
        return cache[name]

    return get


def _galley_flights(args) -> int:
    """The copy-edit lane: propose -> site/filter -> cluster -> judge -> real
    edit-channel findings tagged lane="copyedit". Three modes share this one
    entry point (see the parser help): the full pipeline, --propose-only
    (stop after clustering — no judge spend), and --judge-only (skip straight
    to judging an existing clusters.json, no manuscript needed)."""
    from . import flights as fl
    from .providers import ProviderError

    cfg = load_config(args.config)
    if getattr(args, "variant", None):
        cfg.variant = args.variant
    # Posture: the flag wins when given; otherwise the config's flights
    # section — which is where a genre posture preset lands it.
    if args.posture is None:
        args.posture = cfg.flights.posture
    out = Path(args.out) if args.out else Path(cfg.output_dir)
    usage = Usage()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    lenses = [l.strip() for l in args.lenses.split(",") if l.strip()]
    flight_specs = fl.flight_matrix(models, lenses)

    if args.judge_only:
        setup_logging(out)
        clusters_path = Path(args.judge_only)
        try:
            raw = json.loads(clusters_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read {clusters_path}: {e}", file=sys.stderr)
            return 2
        rows = raw.get("clusters", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            print("error: clusters must be a JSON array (or a "
                  "{'clusters': [...]} envelope)", file=sys.stderr)
            return 2
        clusters = [fl.Cluster.from_json(r) for r in rows if isinstance(r, dict)]
        return _flights_judge_and_write(args, cfg, out, clusters, usage,
                                        source=raw.get("source")
                                        if isinstance(raw, dict) else None)

    if not args.input:
        print("error: an input manuscript is required unless --judge-only is "
              "given", file=sys.stderr)
        return 2

    error_dir = _resolve_error_dir(args.config)
    try:
        prepared = prepare(cfg, args.input, error_dir)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        words = sum(len(p.text.split())
                   for p in fl.usable_paragraphs(prepared.doc.paragraphs))
        proj = fl.project_cost(words, flight_specs, args.judge_model,
                               propose_max_tokens=args.propose_max_tokens,
                               judge_max_tokens=args.judge_max_tokens)
        print(f"\nDry run — {words} reviewable word(s), {len(flight_specs)} "
              f"flight(s): {', '.join(s.key for s in flight_specs)}")
        for row in proj["flights"]:
            print(f"  propose {row['flight']:<32} ~${row['est_usd']:.3f}  "
                  f"(~{row['est_candidates']} candidate(s))")
        print(f"  judge   {args.judge_model:<32} ~${proj['judge_usd']:.3f}  "
              f"(~{proj['est_clusters']} cluster(s), from an est. "
              f"{proj['est_total_candidates']} candidate(s))")
        print(f"\n  PROJECTED total: ~${proj['total_usd']:.2f}")
        if args.json:
            print(json.dumps(proj, ensure_ascii=False))
        return 0

    setup_logging(out)
    try:
        provider_of = _flights_provider_of(cfg)
        # Resolve every flight's provider up front so a missing key fails
        # before any propose call is made, not partway through the matrix.
        for spec in flight_specs:
            provider_of(spec.model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    paragraphs = prepared.doc.paragraphs
    closing_quotes = (prepared.variant.closing_quotes if prepared.variant
                      else "”\"")
    text_of = {p.para_id: p.text for p in fl.usable_paragraphs(paragraphs)}

    by_flight = fl.propose_flights(
        paragraphs, provider_of, flight_specs,
        max_tokens=args.propose_max_tokens, usage=usage,
        closing_quotes=closing_quotes, concurrency=args.concurrency)

    if args.external_proposals:
        ext_path = Path(args.external_proposals)
        try:
            ext_raw = json.loads(ext_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: could not read {ext_path}: {e}", file=sys.stderr)
            return 2
        ext_rows = (ext_raw.get("proposals", ext_raw)
                   if isinstance(ext_raw, dict) else ext_raw)
        if not isinstance(ext_rows, list):
            print("error: --external-proposals must be a JSON array (or a "
                  "{'proposals': [...]} envelope)", file=sys.stderr)
            return 2
        ext_cands, ext_dropped = fl.load_external_proposals(
            ext_rows, text_of, closing_quotes=closing_quotes)
        for c in ext_cands:
            by_flight.setdefault(c.flight, []).append(c)
        print(f"  external proposals: {len(ext_cands)} sited, {ext_dropped} "
              f"dropped by the deterministic filters")

    clusters = fl.cluster_proposals(by_flight, text_of)
    agree2 = sum(1 for c in clusters if c.agreement >= 2)
    print(f"\nPropose: {sum(len(v) for v in by_flight.values())} candidate(s) "
          f"across {len(by_flight)} flight(s).")
    print(f"Union: {len(clusters)} cluster(s) ({agree2} by >=2 flights, "
          f"{len(clusters) - agree2} by one).")

    if args.propose_only:
        out.mkdir(parents=True, exist_ok=True)
        clusters_path = out / "clusters.json"
        clusters_path.write_text(json.dumps(
            {"clusters": [c.to_json() for c in clusters], "source": args.input,
             "models": models, "lenses": lenses},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  propose-only: judge skipped, {len(clusters)} un-judged "
              f"cluster(s) written.\n  {clusters_path}")
        if args.json:
            print(json.dumps({"clusters": str(clusters_path),
                              "count": len(clusters)}, ensure_ascii=False))
        return 0

    return _flights_judge_and_write(args, cfg, out, clusters, usage,
                                    source=args.input)


def _flights_judge_and_write(args, cfg, out, clusters, usage, *,
                             source: str | None) -> int:
    """The shared tail of every `flights` mode that reaches the judge: rule on
    every cluster once, accept at the confidence floor, and write the
    findings envelope. `source` is carried through only for the report — a
    cluster is judged from what it carries, not from re-reading the
    manuscript."""
    from . import flights as fl
    from .providers import ProviderError, cost_of_usage

    try:
        provider = _flights_provider_of(cfg)(args.judge_model)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    verdicts = fl.judge_clusters(clusters, provider, model=args.judge_model,
                                 posture=args.posture, usage=usage,
                                 max_tokens=args.judge_max_tokens,
                                 concurrency=args.concurrency)
    accepted, counts = fl.accept(clusters, verdicts,
                                 min_confidence=args.min_confidence)
    ids = itertools.count(1)
    findings = fl.findings_from_accepted(accepted, ids)

    cost = cost_of_usage(usage, fallback_model=args.judge_model) or 0.0
    envelope = {
        "findings": [fl.finding_to_json(f) for f in findings],
        "cost": {"total_usd": round(cost, 4), "by_model": usage.by_model},
        "ledger": {"api_calls": usage.api_calls,
                   "input_tokens": usage.input_tokens,
                   "output_tokens": usage.output_tokens,
                   "cache_read_input_tokens": usage.cache_read_input_tokens,
                   "cache_creation_input_tokens": usage.cache_creation_input_tokens},
        "checkpoint": None,
        "lane": fl.LANE,
        "posture": args.posture,
        "judge_model": args.judge_model,
        "min_confidence": args.min_confidence,
        "clusters": len(clusters),
        "judge_counts": counts.to_json(),
        "source": source,
    }
    out.mkdir(parents=True, exist_ok=True)
    findings_path = out / "flights_findings.json"
    findings_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    print(f"\nJudge ({args.posture}, floor={args.min_confidence}): "
          f"{counts.accepted} accepted, {counts.kept} kept-original, "
          f"{counts.below_floor} below floor, {counts.unjudged} unjudged.")
    print(f"  {len(findings)} finding(s), ${cost:.2f}, {usage.api_calls} "
          f"model call(s).")
    print(f"\n  {findings_path}")
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False))
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


def _envelope(*, findings, usage: Usage, model: str, coverage=None,
             checkpoint=None, extra: dict | None = None) -> dict:
    """Thin call-site wrapper over docproof.contract.build_envelope, so every
    verb's --json code reads the same way. See docproof/contract.py for the
    envelope's shape and the reasoning behind it."""
    from .contract import build_envelope
    return build_envelope(findings=findings, usage=usage, fallback_model=model,
                          coverage=coverage, checkpoint=checkpoint, extra=extra)


def _usage_from_payload(payload: dict) -> Usage:
    """Reconstruct a Usage from a findings.json's "usage" object — the same
    shape `dataclasses.asdict(usage)` produced it in, so this is the read side
    of that write. Used where a caller has no live Usage to hand the envelope
    builder (multi-round review keeps its own driver's totals off this
    module's beaten path); malformed input degrades to an all-zero Usage
    rather than failing the whole --json print over a cost figure."""
    try:
        return Usage(**(payload.get("usage") or {}))
    except TypeError:
        return Usage()


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


def cmd_galley_profile(args) -> int:
    from .config import load_config
    from .genre_profile import build_profile, confirm_with_model

    cfg = load_config(args.config)
    try:
        profile = build_profile(args.input, cfg)
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.model:
        from .formats import get_format
        fmt = get_format(args.input)
        pkg = fmt.preflight(args.input, cfg.tracked_changes_policy)
        doc = fmt.build_document_model(pkg, cfg)
        profile = confirm_with_model(profile, doc.paragraphs,
                                     model=args.model, cfg=cfg)

    if args.out:
        Path(args.out).write_text(profile.model_dump_json(indent=2) + "\n",
                                  encoding="utf-8")
        print(f"wrote {args.out}")

    if args.json:
        print(profile.model_dump_json(indent=2))
        return 0

    print(f"{profile.word_count:,} words, {profile.paragraph_count} "
         f"paragraph(s), {len(profile.chapters)} chapter(s)")
    print(f"dialogue density: {profile.dialogue_density:.1%}")
    if profile.proper_nouns:
        top = ", ".join(f"{p.name} ({p.count})"
                        for p in profile.proper_nouns[:10])
        print(f"proper-noun candidates ({len(profile.proper_nouns)}): {top}"
             + (" ..." if len(profile.proper_nouns) > 10 else ""))
    for tic in profile.tics:
        print(f"tic: {tic.label} x{tic.count}")
    if profile.bespoke_sweep_candidates:
        print(f"{len(profile.bespoke_sweep_candidates)} bespoke-sweep "
             f"candidate(s) — see --json for patterns")
    rl = profile.reading_level
    if rl.ari is not None:
        print(f"reading level: ARI {rl.ari:.1f}"
             + (f", mean word rarity (zipf) {rl.mean_zipf:.2f}"
                if rl.mean_zipf is not None else ""))
    guesses = ", ".join(f"{g.genre} ({g.score:.0%})"
                        for g in profile.genre_guesses)
    print(f"genre guess: {guesses}")
    print(f"recommended posture preset: {profile.recommended_preset}"
         + (" (model-confirmed)" if profile.model_confirmed else ""))
    if profile.model_notes:
        print(f"model notes: {profile.model_notes}")
    return 0


def cmd_galley_genre_pack(args) -> int:
    from .genre import write_genre_pack
    from .genre_profile import Profile

    profile = None
    if args.profile:
        try:
            profile = Profile.model_validate_json(
                Path(args.profile).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"error: --profile {args.profile}: {e}", file=sys.stderr)
            return 2

    try:
        summary = write_genre_pack(
            args.base, args.genre, args.out, profile=profile,
            stage=getattr(args, "stage", None), era=getattr(args, "era", None))
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"wrote {summary['out_path']} — genre '{summary['genre']}' over "
         f"{summary['base_config']}")
    if "stage" in summary:
        print(f"  stage: {summary['stage']} (lanes + locks)")
    if summary.get("stage_lock_violations"):
        print(f"  stage lock WON over genre for: "
              f"{', '.join(summary['stage_lock_violations'])}")
    if summary["overlay_applied"]:
        print(f"  posture: {', '.join(summary['overlay_applied'])}")
    if summary["pending"]:
        for key, value in summary["pending"].items():
            print(f"  NOT applied (no matching config section yet): "
                 f"{key} = {value!r}")
    if "continuity_prompt" in summary:
        print(f"  continuity prompt: {summary['continuity_prompt']}")
    if "genre_scans_applied" in summary:
        print(f"  genre scans: {summary['genre_scans_applied']}")
    if summary["seeded_names_count"]:
        print(f"  seeded {summary['seeded_names_count']} proper noun(s) from "
             f"the profile into consistency.seeded_names / "
             f"spellcheck.allowlist")
    elif args.profile:
        print("  profile had no proper-noun candidates to seed")
    if "reading_level_target_ari" in summary:
        print(f"  reading-level target ARI set to "
             f"{summary['reading_level_target_ari']:.1f} from the profile")
    if "anachronism_era" in summary:
        print(f"  anachronism scan era set to {summary['anachronism_era']} "
              f"(the scan is a no-op without it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
