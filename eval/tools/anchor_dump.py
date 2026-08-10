"""Confirmation dump: WHY does a model's findings fail to anchor?

The baseline showed claude-haiku-4-5 at a 29% anchor-failure rate against
gpt-5.6-luna's 0% — a third of its findings quote `original_text` that is not a
verbatim substring of the paragraph, so `validate_findings` drops them at
`rejected_no_anchor` before they can be scored. That would make a strong
detector look like a weak one.

This tool re-runs the eval for one model, captures every `rejected_no_anchor`
finding, and answers two questions the scorecard can't:

  1. WHY did it fail? — it prints the model's quote beside the real paragraph so
     the near-miss (a straight quote where the text has a curly one, a dropped
     "the", collapsed whitespace) is visible.
  2. Would a softer anchor RECOVER it, and is that a good idea? — it re-tries
     each failed quote under progressively looser matching (whitespace, then
     punctuation folding, then a fuzzy character-coverage threshold) and splits
     every recovery into a recall GAIN (the quote sits on a seeded-error case
     and still contains the labelled span) or a would-be FALSE POSITIVE (the
     quote sits on a trap). Recovering findings indiscriminately would also
     resurrect trap flags, so that split is the whole decision.

Dev-only; not shipped in the wheel. Needs the model's provider key in the
environment (e.g. ANTHROPIC_API_KEY). One run costs a few cents.

    ANTHROPIC_API_KEY=... .venv/bin/python -m eval.tools.anchor_dump \\
        --model claude-haiku-4-5
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

from docproof.config import load_config
from docproof.eval.corpus import CorpusError, check_no_leakage, load_corpus
from docproof.eval.runner import run_eval
from docproof.models import index_paragraphs
from docproof.providers import ProviderError, build_provider

_WS = re.compile(r"\s+")
# Characters the normalizer curls or swaps before ingest, or that a model
# commonly writes the plain way — folding them is a cosmetic match, not a
# semantic stretch.
_FOLD = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", " ": " ", "…": "...",
})


def _ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _fold(s: str) -> str:
    return _ws(s.translate(_FOLD))


def _coverage(quote: str, para: str) -> float:
    """Fraction of the quote's characters found, in order, inside the paragraph.
    1.0 for a verbatim substring; ~0.9 for a one-word paraphrase; low for a
    hallucinated quote. Computed on folded text so a pure punctuation/whitespace
    difference doesn't read as a paraphrase."""
    q, p = _fold(quote), _fold(para)
    if not q:
        return 0.0
    sm = difflib.SequenceMatcher(None, q, p, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(q)


# Recovery ladder, loosest last. Each predicate answers "would THIS anchoring
# strategy locate the quote in the paragraph?"
def _recover_level(quote: str, para: str) -> str | None:
    if _ws(quote) in _ws(para):
        return "whitespace"           # differed only in spacing
    if _fold(quote) in _fold(para):
        return "punctuation"          # + curly quotes / dashes / ellipsis / nbsp
    if _coverage(quote, para) >= 0.90:
        return "fuzzy@0.90"           # a real near-miss paraphrase
    return None                        # genuinely not in the paragraph


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--config", default="config/default.yaml")
    ap.add_argument("--error-dir", default="config/error_types")
    ap.add_argument("--cases-dir", default="eval/cases")
    ap.add_argument("--error-types",
                    help="comma-separated subset of types to score (cheaper)")
    ap.add_argument("--out", help="where to write the JSON dump "
                                  "(default: eval/baselines/anchor_failures_<model>_<date>.json)")
    ap.add_argument("--show", type=int, default=1000,
                    help="max failures to print in full (default: all)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    cfg.api.model = args.model

    try:
        cases = load_corpus(args.cases_dir)
        check_no_leakage(cases, args.error_dir)
    except (CorpusError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.error_types:
        wanted = {t.strip() for t in args.error_types.split(",")}
        cfg.error_types = [sorted(wanted)]
        cases = [c for c in cases if c.error_type in wanted]
        if not cases:
            print("error: no cases match --error-types", file=sys.stderr)
            return 2

    try:
        provider = build_provider(cfg)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"Re-running {len(cases)} cases on {args.model} to capture anchor "
          f"failures…", file=sys.stderr)
    with tempfile.TemporaryDirectory() as work:
        run = run_eval(cfg, cases, args.error_dir, Path(work), provider=provider)

    paras = index_paragraphs(run.doc)
    failures = [f for f in run.findings if f.status == "rejected_no_anchor"]

    rows = []
    for f in failures:
        para = paras.get(f.para_id)
        case = run.built.case_for(f.para_id)
        para_text = para.text if para else ""
        level = _recover_level(f.original_text, para_text) if para else None
        is_trap = bool(case and case.is_clean)
        # A recovery is a real recall gain only if the quote both belongs to a
        # seeded-error case AND still spans the labelled error; otherwise it is a
        # would-be false positive (trap) or a spurious off-span flag.
        span_in_quote = bool(case and case.span
                             and _fold(case.span) in _fold(f.original_text))
        if level is None:
            outcome = "unrecoverable"
        elif is_trap:
            outcome = "would_be_false_positive"
        elif span_in_quote:
            outcome = "recall_gain"
        else:
            outcome = "spurious_or_offspan"
        rows.append({
            "case_id": case.id if case else None,
            "error_type": f.error_type,
            "is_trap": is_trap,
            "coverage": round(_coverage(f.original_text, para_text), 3),
            "recover_level": level,
            "outcome": outcome,
            "quote": f.original_text,
            "paragraph": para_text,
        })

    _report(args.model, run, rows)

    out = Path(args.out) if args.out else Path(
        f"eval/baselines/anchor_failures_{args.model}_{date.today()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"model": args.model, "total_findings": len(run.findings),
         "rejected_no_anchor": len(failures), "failures": rows},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


def _report(model: str, run, rows: list[dict]) -> None:
    total = len(run.findings)
    n = len(rows)
    print(f"\n=== anchor failures — {model} ===")
    print(f"{total} findings, {n} rejected_no_anchor "
          f"({n / total * 100:.0f}%)\n" if total else "no findings\n")

    for r in rows:
        tag = "TRAP" if r["is_trap"] else "err "
        lvl = r["recover_level"] or "—"
        print(f"[{tag}] {r['error_type']:<22} cover={r['coverage']:.2f} "
              f"recover={lvl:<12} {r['outcome']}")
        print(f"      quote: {r['quote']!r}")
        print(f"      para : {r['paragraph']!r}\n")

    # The decision table: what a softer anchor would do, cumulatively.
    def count(pred) -> int:
        return sum(1 for r in rows if pred(r))
    ladder = ["whitespace", "punctuation", "fuzzy@0.90"]
    print("--- if the validator softened the anchor ---")
    seen = set()
    for lvl in ladder:
        seen.add(lvl)
        rec = [r for r in rows if r["recover_level"] in seen]
        gains = sum(1 for r in rec if r["outcome"] == "recall_gain")
        fps = sum(1 for r in rec if r["outcome"] == "would_be_false_positive")
        spur = sum(1 for r in rec if r["outcome"] == "spurious_or_offspan")
        print(f"  through {lvl:<12}: recover {len(rec):>3}  "
              f"(recall gains {gains}, new FPs {fps}, off-span {spur})")
    dead = count(lambda r: r["recover_level"] is None)
    print(f"  unrecoverable (truly not in the paragraph): {dead}")


if __name__ == "__main__":
    raise SystemExit(main())
