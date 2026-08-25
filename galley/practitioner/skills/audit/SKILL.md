---
name: audit
description: Audit a finished wave for missed errors — density anomalies, sampled pages, hypotheses, and the residual estimate for the letter.
---

# /audit — what did we miss, and is another wave worth it?

## Procedure

1. **Density table.** Findings per 1,000 words by chapter
   (`docproof galley audit RESULTS IN`, or build it from the casefile). A
   chapter far below its neighbors was probably under-read; a very dense one
   may have crowded out subtler errors.
2. **Read real pages** from the quiet chapters — never reason from the
   numbers alone. One hypothesis per suspected miss: chapter, error_class
   (specific — `comma_splice`, not "grammar"), one sentence of evidence, a
   span hint, confidence. A short honest list beats a long speculative one —
   every hypothesis is a paid re-read.
3. **Reject-all + artifact audit** if not already run this wave (see
   merge-desk §3–4). New bugs found here become deterministic rules or fix
   plans, not silent hand-edits.
4. **Judgment-quality spot check.** Sample the REJECTED and HELD edits, not
   just the applied ones — a confused meaning gate wrongly holding correct
   mechanical fixes is a known failure mode; promotable holds go back through
   replay. Also sample comments: false-alarm queries burn the comment budget.
5. **Residual estimate (capture–recapture).** Where two independent lanes
   read the same text: with lane A catching a, lane B catching b, overlap m,
   estimated total ≈ a·b/m; residual ≈ estimate − union. Report it in the
   letter honestly. Seeded-recall (`galley seed`/`score`) calibrates the same
   number between books.
6. **Decide.** Hypotheses → targeted single-pass re-reads (chapter × error
   classes), priced like any plan line. Stop when the marginal cost per
   finding crosses the plan's ceiling or the audit comes back quiet. A quiet
   audit is a RESULT, not a failure — say so and converge.

## Checks that have caught real money

- A detector showing $0 spend that should have billed silently didn't run.
- `stop_reason != ok` on any structured call = a loss, never a partial parse.
- Coverage ledger `degraded`/`unruled`/truncation notes — a "done" run that
  left a hole must not read as clean.
