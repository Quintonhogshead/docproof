# Candidate Detector Recovery & Completion

Status of the recovery plan against the shipped candidate-screening subsystem.
Branch: `claude/candidate-detector-recovery-0742a9` (v0.111.0).

> **Release posture:** the subsystem's Apply mode is **release-gated to Shadow**
> until the P4-04 gates pass and the change is approved. Nothing merges or pushes
> without explicit approval.

## The reported failure (root cause)

A correctly punctuated introductory clause was turned into a **double comma**,
and a genuinely missing intro-clause comma was **mis-located** to just after the
conjunction. Three defects combined:

1. `_introductory_candidates` dropped a zero-width insertion anchor *on top of*
   an existing comma, with `observed_text=""` (P1-01).
2. No universal guard rejected inserting punctuation adjacent to identical
   punctuation (P1-02).
3. An empty-`observed` zero-width anchor validated at **any** offset
   (`"" == ""`), so the stale anchor was never caught (P1-03).

## What changed

### P0 — Containment
- `resolve_candidate_mode()` clamps any requested `apply` to `shadow` unless the
  `CANDIDATE_APPLY_RELEASED` gate (or `DOCPROOF_CANDIDATE_APPLY=1`) is set.
  Applied at the single run chokepoint **and** in `production_findings` (defense
  in depth). Standalone, combined, batch, and CLI paths are all contained.
- The double-comma failure captured as a reproducible fixture
  (`tests/fixtures/candidate_double_comma/`) with a build script and README.

### P1 — Correctness
- Intro-clause generator now **passes** on an existing comma and **fails closed**
  (a non-editing query) when the true boundary needs a parser; strong
  single-word intros still insert safely at the boundary.
- Universal insertion guard `text_invariant_violation()` rejects any correction
  that would create adjacent duplicate `,`/`;`/`:`, a doubled space, or a new
  space-before-punctuation — enforced in `validate_correction` (authoritative,
  pre-ERROR) and again in `production_findings` (rejection recorded in the
  ledger).
- Anchor validation hardened: a replacement must match a non-empty observed
  span; a zero-width insertion cannot claim observed text.
- Final-document safety = this guard **plus** the existing strict reject-all
  audit (lost text / formatting damage / recoverability).

### P2 — Recall
- Standalone mode no longer relies on the regex generators alone: the free
  deterministic sweeps, unbalanced-quote, and term-consistency scans are reused
  as **candidates through the ledger** (`reuse_local_analyzers`), never straight
  to the document.
- New generator families: `homophone` (confusables → judged in context),
  `punctuation_style` (space-before-punctuation edits, bracket balance, and the
  adapted dash/ellipsis/terminal/quote sweeps), `term_consistency` (reused
  scan), and an opt-in `grammar` floor (LanguageTool, `languagetool_floor`).
- A **coverage manifest** (`candidate_coverage.py`) declares covered / partial /
  gap families; the report surfaces it so a low-recall run shows its omissions.
- Open-discovery feedback into the ledger is retained.

### P3 — Screening
- Insufficient context now **defers** with an explicit reason instead of being
  judged on partial context (P3-02).
- Candidate **queries can become margin comments** (`production_queries`,
  `force_query`) — a query can annotate the document but never silently becomes
  an edit (P3-05). Gated with Apply.
- Retry handling, risk/overlap escalation, and correction validation were
  audited and kept.

### P4 — Evaluation
- Seeded fixtures per candidate type with clean/adversarial controls
  (`eval/candidate_cases/`), scored by `docproof.eval.candidate_eval`:
  **generation recall and edit false-positive rate by type**.
- `shadow_report()` scores a shadow run over any document, including the existing
  detector corpus (P4-02).
- **Release gates** (`release_gates`) enforce: zero unaccounted candidates, no
  stale-anchor applications, generation recall ≥ threshold, edit FP ≤ threshold.
  Runnable: `python -m docproof.eval.candidate_eval`.

### P5 — UI
- The combined-review toggle runs **Shadow** and says so; Apply is described as
  release-gated. Standalone tier still requests Apply (clamped to Shadow).
- The candidate report / ledger are downloadable results; the job card offers the
  report; the report carries coverage + omissions.

## Evidence

- Generation scorecard (seeded fixtures): **recall 1.0 (13/13), edit
  false-positive rate 0.0 (0/14 controls)** — including every double-comma trap.
- Release gates: **pass**.
- Full test suite: **3074 passed, 1 skipped** (was 3039 before this work; the
  1 skip predates it). Candidate-specific tests: `test_candidate_double_comma.py`
  (11), `test_candidate_generators.py` (22), `test_candidate_screening.py` (26),
  `test_candidate_eval.py` (7).

## Known gaps (honestly reported, not silently uncovered)

Parser-backed grammar (fragments/run-ons/parallelism), dialogue
attribution/speaker transitions, dates/units/percentages, tables/footnotes/
captions/cross-references, and continuity/visual-layout have **no generator
yet** — declared as `partial`/`gap` in the coverage manifest and shown in every
report. LanguageTool grammar is wired but opt-in (JVM cost).

## Not done (needs approval / product decisions)

- P6-02 canary deploy (shadow-first, then opt-in Apply) — a production action.
- Flipping `CANDIDATE_APPLY_RELEASED` — only after gates pass **and** approval.
