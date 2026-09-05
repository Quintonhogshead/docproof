"""Argument definitions for the DocProof command-line interface."""
from __future__ import annotations

import argparse

from . import __version__
from .profiles import PROFILE_KEYS
from .variants import VARIANT_KEYS

DEFAULT_WORKSPACE = "jobs"


def build_parser() -> argparse.ArgumentParser:
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
    inv.add_argument("--para-map", action="store_true",
                     help="print every paragraph's id and the start of its "
                          "canonical text, then exit — the id/anchor reference "
                          "for import-findings / replay / intent-zones rows")
    _stage_arg(inv)
    _genre_arg(inv)
    _profile_arg(inv)

    rev = sub.add_parser("review", help="run the full pipeline now")
    _common(rev)
    rev.add_argument("--dry-run", action="store_true",
                     help="estimate the cost of this exact config and exit — no "
                          "API call, no output written (chunks x models x token "
                          "budgets, plus the continuity reads); the authoritative "
                          "price for the plan gate")
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

    tns = sub.add_parser(
        "tense", help="whole-book narrative-tense profile (no API): baseline "
                      "tense and person, per-paragraph verdicts with dialogue "
                      "stripped, and the contiguous runs that read against "
                      "the baseline — the scenes a targeted re-read should "
                      "cover. A whole scene in the historical present is "
                      "internally consistent and invisible to per-sentence "
                      "checks; it is only visible from above, against the "
                      "book's own baseline. Report-only; nothing is edited.")
    tns.add_argument("input", help="a .docx or .idml file")
    tns.add_argument("--config", default="config/default.yaml")
    tns.add_argument("--json", action="store_true",
                     help="print the machine-readable profile instead of the "
                          "summary (redirect to a file to keep it)")

    cit = sub.add_parser(
        "cites", help="citation & cross-reference check (no API, "
                      "nonfiction/academic): author-date citations matched "
                      "against the reference list both ways, plus chapter, "
                      "figure, and table cross-references resolved against "
                      "the book's own headings and captions. Report-only — "
                      "raise queries from it; nothing is edited, nothing is "
                      "fact-checked or restyled. Checks whose scaffolding "
                      "the book lacks (no reference list, no numbered "
                      "chapters, no captions) auto-skip.")
    cit.add_argument("input", help="a .docx or .idml file")
    cit.add_argument("--config", default="config/default.yaml")
    cit.add_argument("--json", action="store_true",
                     help="print the machine-readable report instead of the "
                          "summary (redirect to a file to keep it)")

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
    imf.add_argument("--anchor", choices=["source", "accepted"],
                     default="source",
                     help="what text the rows quote: the SOURCE manuscript "
                          "(default), or the ACCEPTED text of an earlier build "
                          "(--run) — a row found while reading the finished "
                          "text. Accepted rows are translated through that "
                          "build's edit map: one on untouched text becomes a "
                          "new edit, one inside an applied edit's span REVISES "
                          "that edit (a composite), one that changes a "
                          "number/name/title/date becomes a query, and one "
                          "that is only an editorial note is dropped")
    imf.add_argument("--run", metavar="RUN",
                     help="with --anchor accepted: the finished build (dir "
                          "holding findings.json + the .docx) whose accepted "
                          "text the rows quote; its kept rows are replayed "
                          "with yours folded in")
    imf.add_argument("--after-sweeps", action="store_true",
                     help="the rows were captured AFTER the deterministic sweeps "
                          "ran (an en-dash, a lowered am, an added :00), so "
                          "re-anchor them against the swept text and rewrite each "
                          "to the equivalent pre-sweep quote — no hand-built "
                          "micro-spans")
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
    rpl.add_argument("--after-sweeps", action="store_true",
                     help="the rows were captured AFTER the deterministic sweeps "
                          "ran, so re-anchor them against the swept text and "
                          "rewrite each to the equivalent pre-sweep quote")
    rpl.add_argument("--json", action="store_true",
                     help="also print the machine-readable result envelope "
                          "to stdout")

    _galley_parser(sub)

    cap = sub.add_parser(
        "capabilities",
        help="print the command tree (verbs + one-line help + config section "
             "names) as JSON — so a practitioner discovers what exists without "
             "reading --help or the source. $0")
    cap.add_argument("--json", action="store_true",
                     help="the default output IS json; accepted for symmetry")
    return ap


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
    ga.add_argument("--seed", type=int, default=0,
                    help="RNG seed for the page sample and the control chapter "
                         "(default: 0); the same seed replays the same sample")
    ga.add_argument("--out", help="where to write audit.json "
                                  "(default: the results directory)")
    _galley_spend_args(ga)
    ga.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")

    gv = gsub.add_parser(
        "verify", help="read a finished run's ACCEPTED text for SENSE — the "
                       "change verifier (each applied edit) + the finished-text "
                       "walk (residual errors); the gates certify cannot run")
    gv.add_argument("results", help="the finished run's output directory (the "
                                    "one holding findings.json and the .docx)")
    gv.add_argument("--config", default="config/default.yaml")
    gv.add_argument("--model",
                    help="the verifying model (default: the continuity reader if "
                         "one is configured, else the review model)")
    gv.add_argument("--context", help="a file of house-style / voice notes fed "
                                      "to both gates (optional; e.g. a BRIEF)")
    gv.add_argument("--changes-only", action="store_true",
                    help="run only the change verifier")
    gv.add_argument("--walk-only", action="store_true",
                    help="run only the finished-text walk")
    gv.add_argument("--out", help="where to write change_verify.json and "
                                  "finished_walk.json (default: the results dir)")
    gv.add_argument("--dry-run", action="store_true",
                    help="count and price the model calls the two gates would "
                         "make (one per 30 applied edits, one per ~6k chars of "
                         "accepted text); no API call, no keys, nothing written")
    _engine_arg(gv)
    gv.add_argument("--paragraphs", metavar="IDS",
                    help="verify ONLY these paragraphs (comma-separated para "
                         "ids, or @FILE holding one per line) — the delta "
                         "re-read the settle loop runs after a round")
    _galley_spend_args(gv)
    gv.add_argument("--json", action="store_true",
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
    gl.add_argument("--workspace", metavar="WS",
                    help="a Galley workspace: the letter's spend and waves "
                         "are read from EVERY run under WS/runs/ (findings."
                         "json cost plus settlement/verify artifacts), not "
                         "only the run the casefile came from — a $0 replay "
                         "build otherwise reports \"$0.00 spent, 1 wave\"")
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
                           "directory — or, for a bare `review`/`replay` run "
                           "that wrote none, from its findings.json cost "
                           "envelope (source is still required, to resolve "
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
    glp.add_argument("--chapter-rows", metavar="ROWS_JSON",
                     help="write the chapter/part label renumbering rows "
                          "(import-findings shape) to this path — labels out "
                          "of sequence or style are mechanics, fixed as "
                          "tracked heading edits, never queried")
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
    glg.add_argument("--corrections",
                     help="a profile-corrections overlay JSON (from `docproof "
                          "galley triage-nouns` + human edits): only its "
                          "protect/enforce names seed the config; reject/"
                          "suspect names are held back. The raw profile is "
                          "untouched.")
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
    gf.add_argument("--judge-model", default=None,
                    help="the judge — one call per cluster, ~90%% of the "
                         "lane's cost (default: the config's flights judge, "
                         "gpt-5.6-luna). A Claude model named here BILLS via "
                         "the API — the $0 Claude route is a session subagent "
                         "over export-judgments / import-judgments")
    gf.add_argument(
        "--posture", choices=["strict", "lenient"], default=None,
        help="strict defaults to keeping the original (measured ~24%% of "
             "proposals accepted); lenient leans toward accepting (~57%%). "
             "Same proposals, same hard vetoes (voice/meaning/fragment/"
             "lateral-swap) either way — only the default and how generously "
             "'defensible' reads moves. Genre tailoring sets this per "
             "manuscript via flights.posture (default: lenient — the "
             "copy-edit lane offers, the author decides)")
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
    _galley_spend_args(gf)
    gf.add_argument("--variant", choices=list(VARIANT_KEYS))
    gf.add_argument("--json", action="store_true",
                    help="also print the machine-readable result to stdout")

    gxj = gsub.add_parser(
        "export-judgments",
        help="build a canonical judgment PACKET from a clusters.json (from "
             "flights --propose-only) for an external judge — a session agent "
             "or a human — to rule on. $0: no model, no keys. Pairs with "
             "import-judgments")
    gxj.add_argument("clusters", help="a clusters.json from "
                                      "`galley flights --propose-only`")
    gxj.add_argument("--out", required=True,
                     help="where to write the judgment packet JSON")
    gxj.add_argument("--intent-zones", metavar="ZONES_JSON",
                     help="a JSON mapping para_id -> [[start,end],...] of "
                          "protected char ranges; a cluster whose span "
                          "intersects one is flagged intent_zone so the import "
                          "refuses to EDIT it (query/reject only)")
    gxj.add_argument("--json", action="store_true",
                     help="also print the machine-readable result to stdout")

    gij = gsub.add_parser(
        "import-judgments",
        help="the model-free judge route: validate a filled judgment packet "
             "(anchoring, atomicity, allowed channel, intent-zone compliance) "
             "and write the findings envelope — NO model call, unlike "
             "flights --judge-only. Pairs with export-judgments")
    gij.add_argument("packet", help="a judgment packet JSON with decisions "
                                    "filled in by the external judge")
    gij.add_argument("--out", help="output directory for flights_findings.json "
                                   "(default: beside the packet)")
    gij.add_argument("--json", action="store_true",
                     help="also print the machine-readable result to stdout")

    grt = gsub.add_parser(
        "routes",
        help="the effective-config egress report: every model the config "
             "would CALL, the provider that serves it, and whether its lane is "
             "active. $0. --deny PROVIDER fails if that provider is reachable")
    _stage_arg(grt)
    _genre_arg(grt)
    grt.add_argument("--config", default="config/default.yaml")
    grt.add_argument("--deny", action="append", default=[], metavar="PROVIDER",
                     help="fail (exit 3) if any ACTIVE route reaches this "
                          "provider (repeatable) — a data-egress preflight")
    grt.add_argument("--json", action="store_true",
                     help="print the routes as JSON")

    gap = gsub.add_parser(
        "approve",
        help="write the immutable approval manifest (approval.json) from a "
             "human-approved plan: source + config hashes, allowed models/"
             "providers, stage, enabled lanes, max spend. $0")
    gap.add_argument("input", help="the manuscript this approval is for")
    _stage_arg(gap)
    _genre_arg(gap)
    gap.add_argument("--config", default="config/default.yaml")
    gap.add_argument("--budget", type=float, required=True,
                     help="the approved maximum spend in USD")
    gap.add_argument("--out", default="approval.json",
                     help="where to write the manifest (default: approval.json)")
    gap.add_argument("--note", default="", help="a free-text note recorded in "
                                                "the manifest")
    gap.add_argument("--comment-budget", type=int, default=None,
                     help="the margin-comment ceiling this run promises "
                          "(about 1 per 1,000 words); certify FAILS a "
                          "deliverable carrying more. Omitted: certify reads "
                          "the workspace profile's figure")
    gap.add_argument("--mechanical-only", action="store_true",
                     help="record the go-live scope on the manifest: "
                          "mechanical proofreading only. Refuses to write an "
                          "approval over a config that enables a copy-edit "
                          "lane (smoothing/rewrite), and makes `galley "
                          "certify` FAIL a run that grew one")
    gap.add_argument("--json", action="store_true")

    gct = gsub.add_parser(
        "certify",
        help="the delivery gate: re-check a finished run against its approval "
             "and the structural invariants (hashes, approved routes, "
             "checkpoint, zero-cost anomaly, budget, artifact scan). Exit 4 if "
             "any check fails. $0")
    gct.add_argument("run", help="the finished run's output directory")
    gct.add_argument("--approval", help="the approval.json to certify against "
                                        "(optional; hash/route/budget checks "
                                        "are skipped without it)")
    gct.add_argument("--source", help="the manuscript, to check the source "
                                      "hash against the approval and to tell "
                                      "a pre-existing text-hygiene fault from "
                                      "one this run introduced")
    gct.add_argument("--config", help="the effective config, to check the "
                                      "config hash and routes")
    _stage_arg(gct)
    _genre_arg(gct)
    gct.add_argument("--json", action="store_true",
                     help="print the certificate as JSON")

    gtn = gsub.add_parser(
        "triage-nouns",
        help="group a profile's proper-noun candidates into protect/enforce/"
             "reject/suspect (with counts, reasons, and near-match flags like "
             "Deut/Deute) — the first move of building a correction overlay. $0")
    gtn.add_argument("profile", help="a profile JSON from `galley profile`")
    gtn.add_argument("--out", help="write a correction-overlay STARTER JSON "
                                   "here (proper_noun_classes prefilled with "
                                   "the suggestions for you to edit)")
    gtn.add_argument("--json", action="store_true",
                     help="print the triage table as JSON")

    giz = gsub.add_parser(
        "intent-zones",
        help="resolve an intent-zones file against a manuscript and preview the "
             "protected spans + permission classes. --out writes the resolved "
             "{para_id: [[start,end]]} map that export-judgments consumes. $0")
    giz.add_argument("input", help="a .docx or .idml file")
    giz.add_argument("--zones", required=True,
                     help="an intent-zones JSON ({'zones': [...]}); see "
                          "docproof/intent_zones.py for the selector schema")
    giz.add_argument("--config", default="config/default.yaml")
    giz.add_argument("--out", help="write the resolved para->spans map here")
    giz.add_argument("--json", action="store_true",
                     help="print the resolved map as JSON")

    gld = gsub.add_parser(
        "ledger",
        help="the finding lifecycle ledger: reconstruct every finding's stable "
             "id and state (detected/merged/queried/rejected/dropped) from a "
             "finished run, with a duplicate report. $0")
    gld.add_argument("run", help="a finished run directory (holding "
                                 "findings.json) or a findings.json file")
    gld.add_argument("--out", help="write the ledger JSON here")
    gld.add_argument("--json", action="store_true",
                     help="print the full ledger as JSON")

    gst = gsub.add_parser(
        "state",
        help="the resumable run state machine: show the current state, advance "
             "it (hash-stamped), or verify it is safe to resume. $0")
    gst.add_argument("run", help="the run workspace (holds state.json)")
    gst.add_argument("--advance", metavar="STATE",
                     help="advance to this run state (forward-only); creates "
                          "state.json if absent")
    gst.add_argument("--source", help="stamp/verify this manuscript's hash")
    gst.add_argument("--config", help="stamp/verify this config's hash "
                                      "(with --stage/--genre applied)")
    _stage_arg(gst)
    _genre_arg(gst)
    gst.add_argument("--at", default="", help="timestamp to record (you supply "
                                              "it; never read from a clock)")
    gst.add_argument("--by", default="", help="who is advancing the state")
    gst.add_argument("--verify-resume", action="store_true",
                     help="check the current source/config hashes still match "
                          "what the state was recorded against; exit 6 on drift")
    gst.add_argument("--results", metavar="RUN",
                     help="the finished run dir; with --advance settled (or "
                          "later) the advance REFUSES (exit 7) while any "
                          "finding is non-terminal or any verify item has no "
                          "settlement record")
    gst.add_argument("--json", action="store_true")

    gse = gsub.add_parser(
        "settle",
        help="residual settlement: close EVERY open item the verify gates "
             "raised — absorb into the owning edit, add a new edit, drop with "
             "a reason, or query the author — rebuilding at $0 and re-verifying "
             "the touched paragraphs, until nothing is open or --rounds is "
             "spent (leftovers ship as questions). Writes settlement.json and "
             "outcome.json; leaves the run in a state certify can pass")
    gse.add_argument("run", help="the finished run dir (findings.json, the "
                                 ".docx, finished_walk.json/change_verify.json)")
    gse.add_argument("--source", required=True,
                     help="the manuscript the run reviewed (.docx/.idml)")
    gse.add_argument("--config", default="config/default.yaml",
                     help="the $0 replay config the rebuilds run under")
    _stage_arg(gse)
    _genre_arg(gse)
    gse.add_argument("--rounds", type=int, default=None,
                     help="settle rounds before leftovers become queries "
                          "(default 3). With --until-clean it is the CEILING "
                          "on the sweep — `--until-clean --rounds 3` stops "
                          "after three rounds even if the third is still "
                          "noisy; left unset, --until-clean sweeps to a quiet "
                          "round, the turn budget, or 12 rounds")
    gse.add_argument("--until-clean", action="store_true",
                     help="keep sweeping while rounds keep finding real work: "
                          "stop after a QUIET round (new items at or below "
                          "--quiet-floor, or at or below --quiet-share of what "
                          "the round re-read), or when --max-turns is spent; "
                          "leftovers ship as questions either way")
    gse.add_argument("--quiet-floor", type=int, default=3,
                     help="a round raising this many new items or fewer is "
                          "quiet (default 3)")
    gse.add_argument("--quiet-share", type=float, default=0.02,
                     help="a round raising at most this share of the edits + "
                          "paragraphs it re-read is quiet (default 0.02)")
    gse.add_argument("--max-turns", type=int, default=400,
                     help="model calls this invocation may make before it "
                          "stops and ships leftovers as questions (default "
                          "400)")
    gse.add_argument("--no-propagate", action="store_true",
                     help="do not apply a settled fix to identical untouched "
                          "occurrences in the same and neighbouring paragraphs")
    gse.add_argument("--mechanical-only", action="store_true",
                     help="proofread scope: a walker suggestion may become an "
                          "edit only when it changes punctuation/case/spacing "
                          "or at most one function word or spelling; anything "
                          "larger ships as a query carrying the suggestion. "
                          "Implied by an --approval whose manifest says "
                          "mechanical_only")
    _engine_arg(gse)
    gse.add_argument("--context", help="a file of house-style / voice notes "
                                       "for the judge and the delta verify")
    gse.add_argument("--no-verify", action="store_true",
                     help="skip the per-round delta re-read (deterministic "
                          "settlement only; the run keeps its last verify "
                          "verdicts)")
    gse.add_argument("--dry-run", action="store_true",
                     help="list the open items with their owner resolution "
                          "and the upper-bound call count; nothing written")
    gse.add_argument("--done-value", default=None,
                     help="HubSpot value outcome.json carries for done "
                          "(default 'Proofing Complete')")
    gse.add_argument("--needs-human-value", default=None,
                     help="HubSpot value outcome.json carries for needs_human "
                          "(default 'Needs Human PR')")
    _galley_spend_args(gse)
    gse.add_argument("--json", action="store_true")

    grs = gsub.add_parser(
        "residuals",
        help="list every open item — residual or flagged edit — with its "
             "owner resolution, so a practitioner (or a test) can see what "
             "`galley settle` will face. $0")
    grs.add_argument("run", help="the finished run dir")
    grs.add_argument("--source", help="the manuscript, to resolve owners "
                                      "through the edit map (optional)")
    grs.add_argument("--config", default="config/default.yaml")
    _stage_arg(grs)
    _genre_arg(grs)
    grs.add_argument("--json", action="store_true")

    goc = gsub.add_parser(
        "outcome",
        help="the terminal verdict: done (no more errors the loop can find or "
             "decide) or needs_human (major grammatical problems; most "
             "sentences must be rewritten) with the reason, written to "
             "outcome.json with the HubSpot property/value for the watch to "
             "flip. $0")
    goc.add_argument("run", help="the finished (settled) run dir")
    goc.add_argument("--source", help="the manuscript (for word/paragraph "
                                      "counts; else read from the deliverable)")
    goc.add_argument("--config", default="config/default.yaml")
    goc.add_argument("--set", choices=["done", "needs_human"],
                     help="overrule the assessment with this verdict "
                          "(requires --reason)")
    goc.add_argument("--reason", help="why, when overruling")
    goc.add_argument("--rewrite-share", type=float, default=None,
                     help="needs_human threshold: share of paragraphs needing "
                          "rewrite-class work (default 0.50)")
    goc.add_argument("--edit-density", type=float, default=None,
                     help="needs_human threshold: applied edits per 1,000 "
                          "words (default 60)")
    goc.add_argument("--done-value", default=None)
    goc.add_argument("--needs-human-value", default=None)
    goc.add_argument("--json", action="store_true")

    gjn = gsub.add_parser(
        "journal",
        help="the decision log: every action taken on the book and why, "
             "rendered from the run's own artifacts (driver events, plan gate, "
             "sweeps, every edit with its recorded reason, verify, settle, "
             "certify, outcome). Deterministic, $0, regenerable any time.")
    gjn.add_argument("run", help="the run directory (the one holding "
                                 "findings.json)")
    gjn.add_argument("--workspace", help="the book's workspace, for PLAN.md, "
                                         "approval.json, profile.json, the "
                                         "driver ledger and certify.txt")
    gjn.add_argument("--book", default="", help="the book's name for the "
                                                "heading (default: read from "
                                                "the approval or the run)")
    gjn.add_argument("--out", help="write here (default: stdout). The driver "
                                   "writes deliverable/DECISION_LOG.md")
    gjn.add_argument("--at", default="", help="timestamp to stamp on it (you "
                                              "supply it; never read from a "
                                              "clock, so the render is "
                                              "reproducible)")

    gag = gsub.add_parser(
        "agent",
        help="the long-running proofing agent: poll the DocProof app for books "
             "awaiting a practitioner, download each Book 1, run the driver "
             "over it, and let the hand-off put the Book 2 set back in the "
             "author's folder. Runs on the machine that holds the Claude "
             "subscription; --install keeps it running at boot.")
    gag.add_argument("--env-file", default=None, metavar="PATH",
                     help="the credentials file (default: ~/.galley/agent.env; "
                          "mode 600 or the agent refuses to start). Holds "
                          "CLAUDE_CODE_OAUTH_TOKEN, GALLEY_APP_URL and "
                          "GALLEY_AGENT_TOKEN — never the shell")
    gag.add_argument("--workspace-root", default=None, metavar="DIR",
                     help="where per-book workspaces, the ledger and the log "
                          "live (default: ~/galley-workspaces)")
    gag.add_argument("--poll-interval", type=float, default=None,
                     metavar="SECONDS",
                     help="how often to ask the app for awaiting books "
                          "(default: 300)")
    gag.add_argument("--budget", type=float, default=None, metavar="USD",
                     help="the API ceiling per book (default: the driver's $10)")
    gag.add_argument("--drive-folder-id", default="",
                     help="deliver every hand-off HERE instead of the author "
                          "folder the app names — for a rehearsal")
    gag.add_argument("--once", action="store_true",
                     help="poll once and exit, instead of running forever")
    gag.add_argument("--status", action="store_true",
                     help="print what this machine has claimed, finished and "
                          "failed, and exit")
    gag.add_argument("--install", action="store_true",
                     help="write and start the service that keeps the agent "
                          "running (a launchd LaunchAgent on macOS, a systemd "
                          "user unit on Linux); also refreshes "
                          "~/galley-bin/galley-run.sh from the repo's copy")
    gag.add_argument("--uninstall", action="store_true",
                     help="stop and remove that service")
    gag.add_argument("--json", action="store_true",
                     help="print the machine-readable result to stdout")

    gdr = gsub.add_parser(
        "drive",
        help="the UNATTENDED driver: seed the per-book workspace and run the "
             "practitioner phases in order, each as its own headless session, "
             "decide the plan gate under --approve, stop on the first failure "
             "with runs/outcome.json = needs_human, and hand the deliverable "
             "off to DocWatch. Mechanical proofreading only by default.")
    gdr.add_argument("--book", required=True,
                     help="the manuscript to proofread (.docx)")
    gdr.add_argument("--slug", required=True,
                     help="the workspace name for this book (one per book)")
    gdr.add_argument("--workspace-root", default=None,
                     help="where per-book workspaces live "
                          "(default: ~/galley-workspaces)")
    gdr.add_argument("--budget", type=float, default=None, metavar="USD",
                     help="the API ceiling in USD for the whole book "
                          "(default: $10). Frozen into approval.json as "
                          "max_spend_usd, so every paid verb refuses past it")
    gdr.add_argument("--approve", choices=["auto", "email", "manual"],
                     default="auto",
                     help="the plan gate: `auto` approves a priced, "
                          "mechanical-only plan inside --budget; `email` sends "
                          "it through `galley ask` and polls QUESTIONS.md for "
                          "a reply; `manual` stops at the gate as today")
    gdr.add_argument("--from", dest="from_phase", metavar="PHASE",
                     help="restart the sequence at this phase (the workspace's "
                          "state.json is the ledger)")
    gdr.add_argument("--phases", nargs="+", metavar="PHASE",
                     help="run only these phases, in order")
    gdr.add_argument("--handoff", metavar="DIR",
                     help="where the DocWatch hand-off files are written "
                          "(default: <workspace>/handoff/)")
    gdr.add_argument("--drive-folder-id", default="",
                     help="also upload the hand-off to this Google Drive "
                          "folder, with the watcher's own sign-in "
                          "(`docproof-watch auth`)")
    gdr.add_argument("--copyedit", action="store_true",
                     help="allow the copy-edit phases (flights, reread). OFF "
                          "by default: go-live Galley is mechanical "
                          "proofreading only (owner, 2026-09-03)")
    gdr.add_argument("--model", default=None,
                     help="the brain model for every phase session "
                          "(default: claude-fable-5)")
    gdr.add_argument("--permission-mode", default=None,
                     help="the headless session's permission mode "
                          "(default: acceptEdits)")
    gdr.add_argument("--wrapbin", default=None,
                     help="the directory holding the `docproof` wrapper that "
                          "re-injects API keys for the sifters "
                          "(default: ~/galley-bin)")
    gdr.add_argument("--reply-timeout", type=float, default=None,
                     metavar="HOURS",
                     help="--approve email: how long to wait for a reply "
                          "before stopping as needs_human (default: 6)")
    gdr.add_argument("--poll-interval", type=float, default=None,
                     metavar="SECONDS",
                     help="--approve email: how often to re-read QUESTIONS.md "
                          "(default: 30)")
    gdr.add_argument("--no-state-gate", action="store_true",
                     help="do not require each phase to have advanced "
                          "state.json before the next one runs")
    gdr.add_argument("--max-turns", type=int, default=None, metavar="N",
                     help="turn cap for every phase session "
                          "(default: per-phase, verify 250 / settle 400)")
    gdr.add_argument("--phase-max-turns", action="append", default=[],
                     metavar="PHASE=N",
                     help="turn cap for ONE phase (repeatable); wins over "
                          "--max-turns")
    gdr.add_argument("--timeout", type=float, default=None, metavar="HOURS",
                     help="wall-clock cap for every phase session "
                          "(default: 2h, ladder 3h, verify/settle 4h)")
    gdr.add_argument("--phase-timeout", action="append", default=[],
                     metavar="PHASE=HOURS",
                     help="wall-clock cap for ONE phase (repeatable); wins "
                          "over --timeout")
    gdr.add_argument("--settle-rounds", type=int, default=None, metavar="N",
                     help="the until-clean sweep's ceiling (default 3): a "
                          "sweep still noisy after N rounds ends the run as "
                          "needs_human")
    gdr.add_argument("--settle-quiet-floor", type=int, default=None,
                     metavar="N",
                     help="a settle round raising N new items or fewer is "
                          "quiet and the book is done (default 4, i.e. fewer "
                          "than five)")
    gdr.add_argument("--settle-quiet-share", type=float, default=None,
                     metavar="SHARE",
                     help="settle's percentage quiet rule (default 0 — off, "
                          "so the absolute count decides alone)")
    gdr.add_argument("--no-question-gate", action="store_true",
                     help="do not stop when a phase appends an escalation to "
                          "QUESTIONS.md (by default an unanswerable question "
                          "stops the run as needs_human)")
    gdr.add_argument("--print-prompt", metavar="PHASE",
                     help="print that phase's prompt and exit — what the thin "
                          "shell wrapper uses; nothing is spawned")
    gdr.add_argument("--dry-run", action="store_true",
                     help="seed the workspace and print the phase sequence; "
                          "spawn no session")
    gdr.add_argument("--json", action="store_true",
                     help="print the driver's result envelope to stdout")


def _galley_spend_args(p: argparse.ArgumentParser) -> None:
    """The approval/budget pair shared by the galley verbs that spend outside
    the review ladder (audit, verify, flights). Their models come from the
    command line, not the config's routes, so the gate is over the models the
    verb will actually call — see _galley_spend_guard."""
    p.add_argument(
        "--approval", metavar="APPROVAL_JSON",
        help="an approval.json from `docproof galley approve`. When given, the "
             "verb REFUSES to run (exit 5) if any model it would call is "
             "outside the approval's allowed models/providers, or the planned "
             "spend exceeds the approved cap")
    p.add_argument(
        "--budget", "--max-spend", dest="budget", type=float, metavar="USD",
        help="a spend ceiling for this verb: refuse (exit 5) when the "
             "projected spend exceeds it, and say so loudly afterwards if the "
             "real spend did")


def _engine_arg(p: argparse.ArgumentParser) -> None:
    """Which lane answers a galley verb's model calls: a vendor API provider,
    the $0 Claude-subscription subagent lane (docproof/providers/subagent.py),
    none (deterministic only), or auto (subagent when this machine can run
    it, else provider when a model is configured, else none)."""
    p.add_argument(
        "--engine", choices=["auto", "provider", "subagent", "none"],
        default="auto",
        help="model lane: 'subagent' = a Claude Code turn on the subscription "
             "($0, needs the Agent SDK + a login); 'provider' = the configured "
             "vendor API (bills); 'none' = deterministic only; 'auto' (default) "
             "= subagent if available, else provider, else none")


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
    p.add_argument(
        "--approval", metavar="APPROVAL_JSON",
        help="an approval.json from `docproof galley approve`. When given, the "
             "command REFUSES to run (exit 5) if the manuscript, the effective "
             "config, or any active model route deviates from what was "
             "approved — the immutable-manifest gate.")


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
