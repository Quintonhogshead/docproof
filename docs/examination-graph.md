# Examination graph: phase-one shadow rollout

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

There are three independent rollback levels, from fastest to broadest:

1. For one review, turn off **Examination coverage ledger — shadow** under the
   submission panel's Safety switches.
2. For the whole Fly deployment, set `DOCPROOF_EXAMINATION_GRAPH=0` and restart
   the app. This kill switch wins over both the shipped YAML and a per-run
   switch, so no examination object or artifact is created.
3. Revert the isolated examination-graph commit and redeploy. The implementation
   is kept in one commit specifically so this is a one-command code rollback.

The permanent configuration switch is `examination_graph.enabled` in
`config/default.yaml`. Setting it to `false` restores the pre-ledger runtime and
output set without removing code.

Disabling or reverting the shadow does not require a data migration. Existing
JSONL and coverage files are inert job artifacts and may be retained or deleted
with their result folders under the normal retention policy.
