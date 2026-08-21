# Candidate generation and screening

The candidate lane is an additive recall detector. It creates explicit potential
error sites locally, records every decision in an append-only ledger, and batches
the ambiguous tail for a separate model judgment. In production mode, only
correction-validated error verdicts become `Finding` objects; they then enter the
same validator, overlap arbitration, edit guard, judge gates, tracked-change
writer, and reject-all audit as every other review source.

`candidate_screening.mode` is `off`, `shadow`, or `apply`; it ships as `off`.
The review UI's Candidate detector switch selects `apply`, while `shadow` remains
available in configuration for evaluation without document changes. The
deployment-wide emergency brake is `DOCPROOF_CANDIDATE_SCREENING=0`. Paid
judgment has a second switch, `candidate_screening.judgment_enabled`, so local
generation and coverage accounting can still run without API cost.

The UI exposes the detector in two ways:

- Select the **Candidate detector** tier to run it alone. The server-enforced
  `candidate-only` profile disables ordinary detectors, sweeps, normalization,
  add-on passes, comments, and the examination graph while retaining the edit
  guard and strict audit.
- In Advanced options, enable **Candidate detector — generate, screen & correct**
  to add it to a regular review. Existing deterministic sources keep first claim
  on overlapping spans; the validator records rather than applies the loser.

Both run-now and overnight jobs are supported. A candidate-only overnight job
may have no ordinary vendor batch; its compact judgment packets run when the job
is collected, before the document is assembled.

The initial rollout generates these ten types:

- dialogue-tag punctuation
- quotation balance, including registered ghost sites for expected closing marks
- introductory commas
- direct-address commas
- number style
- currency style
- repeated words
- nearby word echoes
- heading sequence
- list punctuation

Generation preserves the document part, paragraph ID, character offsets, and
virtual location where applicable. IDs are deterministic over candidate type,
canonical anchors, normalized observed text, and proposed correction. Equivalent
candidates merge their generator evidence instead of becoming duplicate work.

## Screening stages

Each generated candidate is recorded before screening. The lane then validates
its anchor, applies exact local rules, forwards only unresolved candidates
through the lightweight combiner, assembles minimal shared context, and groups
compatible sites into packets of up to 200 candidates. Model responses must
partition the submitted IDs exactly. A valid partial response is retried only
for its missing IDs; duplicate or invented IDs fail the packet and leave its
candidates explicitly pending.

High-risk, uncertain, deferred, and overlapping error verdicts are sent to the
configured escalation model. If no escalation model is available, they remain
reviewer-query verdicts in the ledger. Correction validation rechecks the anchor,
rejects unchanged or excessive fixes, and converts meaning-changing repairs into
queries. Query-preferred and virtual candidates never cross the production
bridge, even if an upstream component labels them errors.

## Artifacts and rollback

An enabled run writes:

- `candidate-screening-report.md`
- `candidate-screening-report.json`
- `candidate-screening-ledger.jsonl.gz`

The report includes terminal coverage, local-resolution rate, escalation and
packet failures, anchor failures, model usage and cost, candidate/production
overlap, emitted findings, applied tracked changes, downstream rejections, and
the Finding-to-Candidate evidence map. Human-grounded recall and precision remain
unset until the ledger is compared with human proofreading results; a candidate
error is not labeled a true positive merely because a model confirmed it.

Setting the mode to `off` removes the lane from `prepare`, `run_sync`, batch
collection, and `finish` without a data migration. Existing artifacts are inert
and can remain with the normal job outputs.
