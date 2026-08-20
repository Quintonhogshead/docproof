# Examination graph: phase-one shadow rollout and Phase 1B experiment

The examination graph is deployed as instrumentation around the existing
DocProof review. It records explicit sites and their decisions, but it does not
send findings into validation and cannot create, alter, or approve manuscript
edits. The existing validator and tracked-change writer remain the only path to
the reviewed document.

The shipped Fly configuration enables the shadow by default. Every completed
review can write three additional files into its normal results directory:

- `examination-coverage.md` — the human-readable coverage report exposed on the
  Fly results card.
- `examination-coverage.json` — the machine-readable report embedded in the
  in-browser “See what changed” data.
- `examination-ledger.jsonl.gz` — the gzip-compressed, append-only JSONL event
  stream, including the site definition on its first event. Compression keeps
  high-volume site accounting practical on the Fly volume and Drive archive.

The archive uploads these files with the rest of the job, so the report remains
downloadable from Fly after a local results directory has been recycled.

## Phase 1B: independent judgment

Phase 1B is a paid, admin-only experiment over a deterministic sample of exact
candidate sites. It starts with locally generated spell/adjudication candidates,
builds context packets, and asks a separately configured model for an explicit
pass, error, uncertain, or defer verdict. The default sample is 10%, bounded by
both a site limit and a $2.00 per-manuscript ceiling. Failed calls and sites that
do not fit under the ceiling stay explicitly unresolved; they are never treated
as passes.

The switch appears to Fly administrators as **Independent examination judge —
experiment**. Switching it on moves the timing control to **Right now** and
disables Overnight. The API independently enforces one review round, run-now,
administrator-only. Ordinary Fly users do not receive the switch in the feature
catalog and cannot enable it with a hand-written request.

Judgment remains a shadow lane. Its usage is billed and reported, but its
verdicts cannot become findings, enter validation, or affect the reviewed
manuscript. The web report shows the selected/judged counts, production-versus-
examination comparison, and cost. When the lanes disagree, the job also writes:

- `examination-evaluation.json` — deterministic blinded A/B cases, downloadable
  from the Fly results card.
- `examination-evaluation-key.json` — the separate source answer key, retained
  with the job artifacts but deliberately not offered by the results UI.

## What phase one measures

Phase one adapts the existing sweep, spell, consistency, adjudication, and model
finding paths into stable examination sites. Deterministic sweep obligations
record negative evidence explicitly: a scanned paragraph is locally passed or
locally confirmed. The current model contract returns only findings, so its
paragraph/category obligations remain `needs_judgment` when no matching finding
was returned. Silence is deliberately not counted as a pass.

The coverage report gives generated, terminal, and explicitly pending counts;
current-state totals by site type and generator; sparse graph totals; and any
obligations omitted by the configured site cap. It also lists the phase-two work
that is not represented yet.

## Rollback on Fly

There are four independent rollback levels, from fastest to broadest:

1. To stop paid judgment across Fly while retaining free coverage accounting,
   set `DOCPROOF_EXAMINATION_JUDGMENT=0` and restart. The independent kill
   switch overrides an administrator's per-run choice.
2. For one review, leave **Independent examination judge — experiment** off,
   or turn off **Examination coverage ledger — shadow** to omit all examination
   artifacts.
3. For the whole Fly deployment, set `DOCPROOF_EXAMINATION_GRAPH=0` and restart
   the app. This kill switch wins over both the shipped YAML and a per-run
   switch, so no examination object or artifact is created.
4. Revert the isolated Phase 1B commits and redeploy. Phase 1B is split from the
   Phase 1A ledger commit specifically so the paid lane and its web controls can
   be rolled back without removing the free accounting layer.

The permanent configuration switch is `examination_graph.enabled` in
`config/default.yaml`. Setting it to `false` restores the pre-ledger runtime and
output set without removing code.

Disabling or reverting the shadow does not require a data migration. Existing
JSONL and coverage files are inert job artifacts and may be retained or deleted
with their result folders under the normal retention policy.
