from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
from pathlib import Path

from .analyzer import Analyzer, MockAnalyzer
from .chunker import chunk_document
from .config import load_config
from .error_registry import load_error_types
from .ingest import IngestError, build_document_model, preflight
from .logging_setup import setup_logging
from .models import Usage
from .reassembler import apply_tracked_changes
from .reporting import write_findings_json, write_summary_md
from .validator import validate_findings

log = logging.getLogger("docproof.main")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="docproof",
        description="LLM-assisted grammar review with native Word tracked changes.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory",
                         help="ingest + chunk only (no API): preview a run")
    inv.add_argument("input")
    inv.add_argument("--config", default="config/default.yaml")

    rev = sub.add_parser("review", help="run the full pipeline")
    rev.add_argument("input")
    rev.add_argument("--config", default="config/default.yaml")
    rev.add_argument("--out", help="output directory (default: from config)")
    rev.add_argument("--error-types", help="comma-separated override")
    rev.add_argument("--model")
    rev.add_argument("--min-confidence", choices=["low", "medium", "high"])
    rev.add_argument("--max-chunks", type=int,
                     help="review only the first N chunks (cheap smoke test)")
    rev.add_argument("--mock-findings",
                     help="JSON file of raw findings; skips the API entirely")
    rev.add_argument("--no-comments", action="store_true")

    args = ap.parse_args(argv)
    return {"inventory": cmd_inventory, "review": cmd_review}[args.cmd](args)


def _configure(args):
    cfg = load_config(args.config)
    if getattr(args, "model", None):
        cfg.api.model = args.model
    if getattr(args, "error_types", None):
        cfg.error_types = [k.strip() for k in args.error_types.split(",")]
    if getattr(args, "min_confidence", None):
        cfg.min_confidence = args.min_confidence
    if getattr(args, "no_comments", False):
        cfg.comments = False
    if getattr(args, "out", None):
        cfg.output_dir = args.out
    error_dir = Path(args.config).parent / "error_types"
    return cfg, error_dir


def cmd_inventory(args) -> int:
    cfg, _ = _configure(args)
    setup_logging(cfg.output_dir)
    try:
        pkg = preflight(args.input, cfg.tracked_changes_policy)
    except IngestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    doc = build_document_model(pkg, cfg)
    chunks = chunk_document(doc, cfg)
    total = sum(c.est_tokens for c in chunks)
    print(f"{len(doc.paragraphs)} reviewable paragraphs → {len(chunks)} chunks "
          f"(~{total:,} document tokens)")
    for pid, reason in doc.skipped:
        print(f"  skipped {pid:<24} {reason}")
    return 0


def cmd_review(args) -> int:
    cfg, error_dir = _configure(args)
    out = Path(cfg.output_dir)
    setup_logging(out)

    if not args.mock_findings and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set (or use --mock-findings).",
              file=sys.stderr)
        return 2

    try:
        pkg = preflight(args.input, cfg.tracked_changes_policy)
    except IngestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    doc = build_document_model(pkg, cfg)
    chunks = list(chunk_document(doc, cfg))
    if args.max_chunks:
        chunks = chunks[:args.max_chunks]
        log.info("--max-chunks: reviewing only the first %d chunk(s)",
                 len(chunks))

    canned = None
    if args.mock_findings:
        try:
            canned = json.loads(Path(args.mock_findings).read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: --mock-findings {args.mock_findings}: {e}",
                  file=sys.stderr)
            return 2
        if not isinstance(canned, list):
            print(f"error: --mock-findings must be a JSON array of findings.",
                  file=sys.stderr)
            return 2

    registry = load_error_types(error_dir, cfg.error_types)
    ids = itertools.count(1)
    usage = Usage()
    findings = []
    for et in registry.values():
        if canned is not None:
            analyzer = MockAnalyzer(et, canned, ids)
        else:
            analyzer = Analyzer(cfg, et, ids)
        for chunk in chunks:
            findings.extend(analyzer.analyze_chunk(chunk, usage))

    validated = validate_findings(findings, doc, cfg.min_confidence)
    stats = apply_tracked_changes(pkg, doc, validated, cfg)

    reviewed = out / f"reviewed_{Path(args.input).stem}.docx"
    pkg.save(reviewed)
    write_findings_json(out / "findings.json", doc=doc, findings=validated,
                        usage=usage, cfg=cfg, applied_ids=stats.applied)
    write_summary_md(out / "summary.md", doc=doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied)

    print(f"\n{len(stats.applied)} tracked change(s) applied.")
    for p in (reviewed, out / "summary.md", out / "findings.json",
              out / "run.log"):
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())