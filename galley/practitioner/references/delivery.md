# Certification and delivery

Enrolled workspaces (`astra-review-required.json`) use the driver's final Astra
high review/adjudication. Certification, authorized repair execution, packaging,
and upload afterward are deterministic. Do not launch another Claude editorial
review, change Astra's verdict, or manually run around a failed gate. The
instructions below are also the human-readable contract for legacy phase sessions.

Certification binds source/config/routes, approval/budget, complete checkpoints,
coverage, current change verification and finished walk, terminal findings,
settlement, outcome, and the actual delivered artifact. Every required check
passes; explicitly report skipped checks. Zero-cost anomalies for a paid detector
are not evidence of success. Plan ledger and comment-premise checks must pass.
No internal repair may remain open; do not convert it to an author question.

Read `references/comment-reconciliation.md` when inspecting final comments or
internal-repair evidence. Finding-owned comments are compared to the actual final
text; stale/duplicate questions are removed while preserving original author
comments and distinct author questions. Honor approval's hard comment ceiling;
never raise it to pass. Reconciliation is bound to the delivered DOCX hash.

After certification, copy the certified tracked manuscript; do NOT rebuild or
edit it. Render `docproof galley letter RUN --workspace . --source BOOK --out
 deliverable/` and check the outputs against evidence:

- `letter.md`: what ran, real spend summed across every workspace run, changes by
  kind, choices/reasons, distinct author questions and ids, honest residual and
  coverage limits, preparation disclosure, outcome.
- `style-sheet.md`: voice/scope, conventions and site counts, preserved names,
  spelling decisions still pending, protected passages. An empty-rulings sheet
  when decisions were made is a defect.
- `verification.md`: certificate table (including skips), verification and
  settlement counts, delivered SHA-256, original/final media counts, paired comment
  anchors, untracked preparation, production notes and limits.
- `author-letter.docx`: the author-facing letter beside the manuscript. The driver
  also renders the decision log from the run's artifacts; record actual times
  through the tools, never invented timestamps.

Read the three evidence documents back in bounded sections; do not accept a $0
letter when the ladder billed or a claim of full coverage with unread text.
Copy outcome.json and preserve its reason. The driver creates the clean reading
copy and required handoff artifacts. Advance certified/delivered state with BOTH
source and config hashes only when their gates pass.

A legacy needs_human/stopped handoff preserves the best available edited book,
letters, decision log, and diagnostics for the next proofreader. It does not mean
an uncertified or incomplete proof is ready. An enrolled technical failure blocks
completion while preserving Astra's editorial verdict.

## Legacy assessment thresholds

For unenrolled runs, the engine's default assessment thresholds are: rewrite-class
work in >=50% of reviewable paragraphs; >=60 wording edits per 1,000 words;
author questions in >=25% of reviewable paragraphs; or verifier damage on >=20%
of applied edits. Report the recorded evidence and reason; do not substitute
these heuristics for Astra's editorial verdict in an enrolled run. The outcome
record carries DocWatch's `docproof` value (`Proofing Complete` / `Needs Human PR`).
An authorized human overrule needs its reason recorded through the tool.
