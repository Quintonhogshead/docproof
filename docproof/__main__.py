from __future__ import annotations

import argparse
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
from .models import Usage
from .pipeline import chunk_outline, finish, prepare, run_sync
from .providers import ProviderError, build_provider, estimate_cost

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
                     choices=["indesign", "tracked", "both"],
                     help="which file(s) to write: the InDesign-ready .docx, "
                          "the tracked-changes .docx, or both "
                          "(default: from config)")
    prp.add_argument("--style-sheet",
                     help="a different house style set (YAML). This is how you "
                          "prep for another template without touching code.")
    prp.add_argument("--model")
    prp.add_argument("--no-verify", action="store_true",
                     help="skip the word-for-word check. Not recommended: it "
                          "is the only thing standing between a mis-tagged "
                          "run and a changed manuscript.")
    prp.add_argument("--mock-tags", action="store_true",
                     help="label everything as running text; exercises the "
                          "writers and the verifier with no API call")

    args = ap.parse_args(argv)
    return {"inventory": cmd_inventory, "review": cmd_review,
            "submit": cmd_submit, "status": cmd_status,
            "collect": cmd_collect, "prep": cmd_prep}[args.cmd](args)


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


def _selection(args) -> list[str] | None:
    raw = getattr(args, "only", None)
    if not raw:
        return None
    return [c.strip() for c in raw.split(",") if c.strip()]


def _configure(args):
    cfg = load_config(args.config)
    if getattr(args, "model", None):
        cfg.api.model = args.model
    if getattr(args, "error_types", None):
        cfg.error_types = [[k.strip() for k in group.split("+") if k.strip()]
                           for group in args.error_types.split(",")
                           if group.strip()]
    if getattr(args, "min_confidence", None):
        cfg.min_confidence = args.min_confidence
    if getattr(args, "no_comments", False):
        cfg.comments = False
    if getattr(args, "out", None):
        cfg.output_dir = args.out
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
    print(f"{len(prepared.doc.paragraphs)} reviewable paragraphs → "
          f"{len(prepared.chunks)} chunks (~{doc_tokens:,} document tokens)")
    print(f"{len(cfg.error_type_keys)} error type(s) in "
          f"{len(prepared.groups)} pass(es) → {prepared.request_count} API "
          f"calls, ~{prepared.est_document_tokens:,} document tokens sent")
    for group in cfg.error_type_groups:
        print(f"  pass: {' + '.join(group)}")

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

    provider = None
    if not args.mock_findings:
        try:
            provider = build_provider(cfg)
        except ProviderError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    try:
        prepared = prepare(cfg, args.input, error_dir,
                           max_chunks=args.max_chunks,
                           selection=_selection(args))
    except (IngestError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.mock_findings:
        canned = _load_mocks(args.mock_findings)
        if canned is None:
            return 2
        findings, usage = _run_mock(cfg, prepared, canned)
    else:
        findings, usage = run_sync(cfg, prepared, provider)

    outputs = finish(prepared, findings, usage, cfg, out_dir=out,
                     source_path=args.input)
    print(f"\n{outputs.applied} tracked change(s) applied.")
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

    print(f"\n{outputs.applied} tracked change(s) applied.")
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
    kinds = (list(preplib.OUTPUT_KINDS) if args.output == "both"
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

    try:
        outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=out,
                                 source_path=args.input, outputs=kinds)
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


# --- helpers ------------------------------------------------------------------

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
    for group in prepared.groups:
        analyzer = MockAnalyzer(group, canned, ids)
        for chunk in prepared.chunks:
            found, _ = analyzer.analyze_chunk(chunk, usage)
            findings.extend(found)
    return findings, usage


if __name__ == "__main__":
    sys.exit(main())
