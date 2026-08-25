---
name: merge-desk
description: Merge the mechanical and copy-edit lanes into one deliverable — span claims, rewrite-must-be-clean, artifact scan, two authors.
---

# /merge-desk — one manuscript, two lanes, zero artifacts

Goal: a single tracked-changes deliverable where Word can filter the
proofread and the copy-edit by author name, with no composition artifacts.

## Claim rules (in order)

1. **Composites and clusters claim first, atomically.** A cluster ships whole
   or is withdrawn whole — one faulted member withdraws its siblings (the
   repair channel's rule, now cross-lane).
2. **A rewrite may subsume an overlapped mechanical fix ONLY if clean**: the
   rewritten sentence passes the deterministic sweeps AND LanguageTool with
   zero hits on the contested span. LT unavailable ≠ clean — fail closed, the
   mechanical fix wins.
3. **Otherwise mechanics win overlaps.** A Chicago fix is never lost to
   styling.
4. Non-overlapping edits from both lanes land side by side.

## Procedure

1. `docproof merge MANUSCRIPT --mechanical A.json --copyedit B.json --dry-run
   > runs/merge_ledger.txt 2>&1` → then `grep`/`head` the claim ledger (never
   dump the whole ledger into context): every contested span, the winner, the
   rule. Spot-check subsumptions — a rewrite that swallowed a spelling fix must
   contain the correction in its replacement text.
2. Apply. Two author names (proofreader / copy editor) so the author can
   filter lanes. One span carries exactly one author — Word cannot stack
   pending revisions; if you want split attribution, split the span.
3. **Artifact scan, iterated to clean**: `,,` and doubled punctuation,
   doubled spaces, space-before-punctuation, `…\.`, `”[.,]`, straight marks.
   Same-point insertions from two sources compose into `,,` — dedupe by
   insertion POINT. A span containing an ellipsis char loses to
   `sweep_ellipsis` — claim characters outside it.
4. **Reject-all round trip**: reject every change in a scratch copy; the text
   must be byte-identical to the source. Any drift = an untracked edit
   escaped — find it before delivery.
5. Quote hygiene on replayed rows: corrected_text sanitized to curly; never
   sanitize C-but-not-O (whole-paragraph fake diffs get rejected_oversized
   and lose real fixes — exclude such rows and hand-fix).

## Never

- Merge by hand-editing XML. You write findings and rules; the engine writes
  the document.
- Ship with a dirty artifact scan "to fix in round 2."
- Let the copy-edit lane's author name appear on a mechanical fix (breaks
  the author's ability to bulk-handle lanes).
