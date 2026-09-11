# Per-book resource receipts

The Galley parent supplies a source-bound ledger to its children. New synchronous
API requests, subscription provider requests, and Codex transport receipts write
directly to the same JSONL file. The driver imports its own terminal Claude
session usage. No prompts, manuscript text, authentication material, or inferred
Max token quota belong in this file.

`docproof.resource_ledger.context_env(path, source_sha256, config_sha256,
parent_operation_id=...)` returns the child environment. In-process callers use
`with use_context(env): ...`; provider objects retain this context when workers
run on other threads. The source must match every previous receipt. Each receipt
also records its configuration hash, phase, group, parent, model, transport,
requested effort/output cap, status, and normalized token evidence. API responses
can report an actual model/version and provider response ID.

Configure these engineering ceilings for a shared review group:

| Environment variable | Meaning |
| --- | --- |
| `DOCPROOF_RESOURCE_GROUP` | Stable group, such as `review`, across primary, secondary, and settlement invocations |
| `DOCPROOF_RESOURCE_MAX_CALLS` | Maximum distinct new provider invocations in that group |
| `DOCPROOF_RESOURCE_MAX_OUTPUT_TOKENS` | Maximum charged/reserved output tokens in that group |
| `DOCPROOF_RESOURCE_RESERVATION_OUTPUT_TOKENS` | Optional per-call output reservation when a transport has no supported output-cap argument |

Reservations precede submission under an interprocess lock. A completed result
with measured output replaces its reservation; missing or interrupted usage
retains the reservation. Restarting cannot erase or increase saved ceilings.
`ResourceBudgetExceeded` stops further submissions and leaves unread work open;
it is not a subscription reset signal or an editorial judgment. Requests already
running can finish, and a provider that exceeds its requested cap is recorded at
its actual output rather than clipped to the estimate.

`summarize(path)` reports known token totals, unknown/missing fields, incomplete
attempts, reused receipts, per-model counts, and group reservation balances.
Thinking tokens are included in output, not added again. Codex's cached input is
subtracted from its total input to normalize it with Claude's separate cache
counters. A parent relationship alone does not mean overlapping usage: only
explicit `included_operation_ids` remove child rows already included by a parent
rollup. `record_claude_result` prefers per-model `modelUsage` buckets and does not
also add the overlapping top-level `usage` object.

Receipt IDs distinguish newly submitted attempts from saved results. Reading an
existing Codex result adds a reused event without new compute. File locks, flushed
append operations, and deterministic event IDs protect concurrent writers and
repeat imports; damaged accounting fails closed instead of reporting zero.

Astra's optional background API transport mirrors its authoritative submission
receipt too. It reserves once before the POST, keeps unknown timeout usage open,
and updates that same attempt during GET-only recovery. Reading an already
completed editorial receipt records reuse and never submits another review.

The subscription provider passes configured effort through the installed Agent
SDK's `ClaudeAgentOptions.effort`, overrides inherited effort through
`CLAUDE_CODE_EFFORT_LEVEL`, and passes the requested output cap through
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`. These controls are documented in the official
[Claude Code environment reference](https://code.claude.com/docs/en/env-vars).
They control individual requests; they are not a monthly allowance guarantee.

Limits of this first instrumentation: transport SDK retries may not expose
individual attempt usage, and batch submission/collection is not covered by the
synchronous wrapper. Failed transport counters may be unknown. Complete saved
usage and measured account allowance changes are still needed to benchmark
finished books; API list equivalents cannot be converted into a Max quota.
