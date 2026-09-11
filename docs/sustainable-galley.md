# Bounded Galley orchestration

New mechanical jobs default to code orchestration through `galley drive` and the
agent. Resumes preserve their saved execution mode; existing unversioned jobs
retain session mode, since they lack the new initial reading receipts. `--execution-mode session` preserves the previous driver for controlled
comparison. Explicit legacy copyediting or no-Astra jobs keep session mode unless
code mode is explicitly requested, which is rejected for those unsupported paths.
Existing in-process `Driver` callers retain their prior default for compatibility.

## Retained editorial work

Profiling, the approved Sonnet + Luna detector ensemble, specialized manuscript
reading and the ladder remain agent-led. No detector pass was pruned in this
release. The code-owned verify stage explicitly schedules two complete reads:
mechanics with Opus and slow type-and-compare with Fable, each with its own pass
identity. Each checks the applied edits and the complete accepted manuscript.
Astra still performs the final editorial review through the existing Codex
subscription workflow. API transport remains an explicit alternative.

Approval, audit execution, verification scheduling and settlement scheduling no
longer run inside a supervising Claude session. The audit still calls its
configured reader; its hypotheses are included in Astra's final context. The
existing deterministic certification and packaging remain in place.

## A bounded repair loop

Both full reads must have complete, current, identity-bound evidence before
settlement starts. Validation checks the actual saved response windows, model,
configuration, context, required pass set, and reconstructed findings. An empty
array from a truncated reader is never a clean read.

The controller allows at most two repair rounds. Settlement batches supported
repairs using the preceding efficiency patch, rebuilds in code, and re-verifies
touched paragraphs. When at least 20% of paragraphs carry open issues, the
controller permits another pair of full reads between rounds. This threshold is
an initial routing heuristic to evaluate, not a quality guarantee.

No open items, no progress on the same open IDs, or an exhausted round ceiling
ends repair and moves a complete tracked snapshot to Astra. Remaining editorial
issues remain explicit in the final review. Missing/stale reading evidence or an
interrupted operation blocks operationally; it is not converted into a question
for the author. The legacy "quiet" threshold is never a clean certificate.

Initial full-read artifacts are frozen independently of mutable delta artifacts.
Each command has input/output fingerprints and a completion receipt. Valid
completed commands are reused; incomplete verification can resume validated
windows. An interruption without a trustworthy completion receipt requires
operational reconciliation and retains its reservation. Commands and inputs
cannot be changed silently to acquire a fresh allowance.

## Initial engineering ceilings

| Setting | Default | Scope |
| --- | ---: | --- |
| `--review-rounds` | 2 | Repair cycles, maximum 2 |
| `--review-calls` | 400 | Shared provider invocations across both verification passes and settlement |
| `--review-output-tokens` | 2,000,000 | Shared measured/reserved output across that review group |
| Existing phase time/turn settings | Retained | Persisted across coordinator restarts |

Ceilings are engineering safeguards, not expected consumption or a conversion to
Claude Max subscription quota. Reservations are atomic across workers. Missing
usage keeps the reservation; measured overages remain charged. A restart cannot
increase the original limits. Review budget exhaustion leaves unread work open.
The intake formatter, coordinators, detector calls and final Astra review are
also metered; their existing transport/time limits remain separate.

`runs/driver/driver.json` contains the current resource summary and separately
reports formatting usage from the original-source intake when applicable.
`runs/driver/resources.jsonl` holds per-call evidence; `execution-budget.json`
holds durable coordinator/time reservations. See [resource-ledger.md](resource-ledger.md)
for field definitions and instrumentation limits. Raw token totals do not establish
capacity for 30–40 books per month.

## Author-visible value

Distinct continuity questions remain distinct comments. Summaries use actual
writer receipts and identify unplaced or unknown-delivery comments. Audit
hypotheses preserve their current schema instead of disappearing during report
rendering. Every independent verification output is archived before overwrite;
its candidates enter settlement and any remaining candidates enter Astra's exact
issue index. A settlement query record alone cannot stand in for a Word comment.

Candidate-screening metrics describe observed candidates and unmatched candidates;
they do not claim causal additional errors found from non-causal matching data.

## Rollout and evaluation

This change is built on the earlier settlement efficiency repair. Offline checks
exercise command routing, real Word artifacts, interrupted reads, receipt reuse,
bounded repairs, source/config changes, reporting, and budget accounting. They do
not measure live editorial recall, subscription allowance consumption, or monthly
throughput. Before removing any further reading, compare completed author-visible
results and measured allowance use on representative books, including Aragón's
punctuation pattern. Protect Sonnet + Luna and the final reread during that pilot.
No live worker restart, deployment, or paid benchmark is part of this build.
