#!/usr/bin/env python3
"""Preflight a Compare-vs-human pair before spending on a review.

Given an original manuscript and its human-proofread copy, this reports —
without any API calls — whether the pair is measurable with `docproof compare`
and what a review would cost:

  * Are the human edits recorded as tracked changes? (If the human file has
    zero tracked edits, the changes were accepted; `compare` can't read them
    and you need a pair-differ, not this path.)
  * Do the two files align paragraph-by-paragraph on base text? (Low alignment
    means they are different drafts and most of the book won't be comparable.)
  * What shape are the human edits — proofread-scale (DocProof's lane) or
    developmental rewrites (out of scope)?
  * How many edits fall in channels DocProof handles SILENTLY (whitespace
    collapse, quote curling) and so will never surface as a tracked change —
    these must be discounted from any "only human" miss list.

Usage:
    .venv/bin/python -m eval.tools.compare_preflight ORIGINAL.docx HUMAN.docx

Add `--inventory` to also print the review cost estimate (still free — the
inventory command does not call the API):
    .venv/bin/python -m eval.tools.compare_preflight ORIG.docx HUMAN.docx --inventory
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docproof.trackdiff import compare_edits, extract_edits, open_docx


def _classify(delete_text: str, insert_text: str) -> str:
    """Bucket one edit by shape. Coarse on purpose — this estimates the
    in-taxonomy ceiling, it does not adjudicate individual edits."""
    d, i = delete_text or "", insert_text or ""
    ds, isr = d.strip(), i.strip()
    if not ds and not isr:
        return "whitespace-only (DocProof normalizes silently)"
    if any(q in d + i for q in "“”‘’\"'"):
        return "quote/apostrophe (partly silent-normalized)"
    if d and not i:
        return "pure deletion (extra/repeated word)"
    if i and not d:
        return "pure insertion (missing word/punct)"
    if ds.lower() == isr.lower() and ds != isr:
        return "capitalization"
    if set(d) <= set(" —–-") or set(i) <= set(" —–-"):
        return "dash/space"
    if len(d) <= 15 and len(i) <= 15:
        return "word/short substitution"
    return "longer rewrite (out of DocProof scope)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", help="the clean original .docx")
    ap.add_argument("human", help="the human-proofread .docx (tracked changes)")
    ap.add_argument("--inventory", action="store_true",
                    help="also print the review cost estimate (no API calls)")
    ap.add_argument("--model", default="gpt-5.6-luna")
    args = ap.parse_args(argv)

    for label, path in (("original", args.original), ("human", args.human)):
        if not Path(path).is_file():
            print(f"error: {label} file not found: {path}", file=sys.stderr)
            return 2

    orig = extract_edits(open_docx(args.original))
    human = extract_edits(open_docx(args.human))

    print(f"original : {len(orig.base):>6} paragraphs, {orig.edit_count:>5} tracked edits")
    print(f"human    : {len(human.base):>6} paragraphs, {human.edit_count:>5} tracked edits")

    if human.edit_count == 0:
        print("\n⚠  The human file has NO tracked changes — the edits were "
              "accepted into clean text.\n   `docproof compare` cannot read this. "
              "This pair needs a pair-differ\n   (Phase B), not the Compare path.")
        return 1

    # Alignment: score human against original as ground truth just to learn how
    # many paragraphs are comparable (base text matches).
    rep = compare_edits(human, orig, label_a="human", label_b="original")
    aligned = rep.aligned_paras
    skipped = len(rep.unaligned)
    total_para = aligned + skipped
    pct = (100 * aligned / total_para) if total_para else 0
    print(f"\nalignment: {aligned} paragraphs aligned, {skipped} skipped "
          f"(base differs) — {pct:.0f}% comparable")
    if pct < 90:
        print("   ⚠  Low alignment: these look like different drafts; most of "
              "the book won't be scored.")

    cats: dict[str, int] = {}
    for pid, edits in human.edits.items():
        for e in edits:
            k = _classify(e.delete_text, e.insert_text)
            cats[k] = cats.get(k, 0) + 1
    tot = sum(cats.values())
    print(f"\nhuman edit shapes ({tot} edits):")
    for k, v in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {k:48} {v:5}  ({100 * v / tot:.0f}%)")

    silent = sum(v for k, v in cats.items() if "silent" in k.lower())
    out_of_scope = sum(v for k, v in cats.items() if "out of" in k.lower())
    print(f"\n  ~{silent} edit(s) in silent-normalization channels — discount "
          f"these from any 'only human' miss list.")
    print(f"  ~{out_of_scope} edit(s) out of DocProof scope (developmental).")

    if args.inventory:
        print("\n--- review cost estimate (no API calls) ---")
        from docproof.__main__ import main as docproof_main
        docproof_main(["inventory", args.original, "--model", args.model])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
