# Final Astra review

Final review uses `gpt-6-astra` with high reasoning through the worker's Codex
subscription by default. Explicit API transport remains available. Neither route
falls back to the other when authentication, capacity, or a request fails.

## Run and resume

```sh
docproof galley astra-review RUN --dry-run --json
docproof galley astra-review RUN --chunk-bytes 180000
docproof galley astra-review RUN --transport api --budget 25 --max-output-tokens 32768
```

`--docx FILE` selects the tracked manuscript; repeat `--context FILE` for additional
approved context. `--json` returns the plan or receipt. Driver options are
`--astra-transport codex|api`, `--astra-chunk-bytes`, `--astra-budget`, and
`--astra-max-output-tokens`. Dollar and output-token ceilings apply to API
transport. Subscription usage consumes the signed-in account's allowance.

Dry-run is offline: subscription mode plans complete evidence ownership and
request counts; API mode estimates a conservative token/cost bound. It does not
submit a review, query paid services, or enroll a run.

The selected transport and chunk size are recorded in
`astra-review-required.json`. After a request exists, conflicting explicit options
are rejected. Omitting transport resumes the saved route; old receipts without a
transport field remain API requests. New unconfigured runs use Codex. Authentication
or delivery problems are operational blocks, never a fabricated human-PR verdict.

## Worker authentication

Use a dedicated persistent authentication directory. Fly sets
`GALLEY_CODEX_HOME=/data/galley-codex`; the local default is `~/.galley/codex`.
Authenticate that directory with a ChatGPT subscription:

```sh
CODEX_HOME="$GALLEY_CODEX_HOME" codex -c 'cli_auth_credentials_store="file"' -c 'forced_login_method="chatgpt"' login --device-auth
```

Set `GALLEY_CODEX_HOME` to the intended directory before this command. Complete the
browser sign-in and code entry yourself. Device login may need enabling in account
or workspace settings. The runner checks the login, forces ChatGPT authentication,
removes inherited API keys, and serializes access to the refreshable cache. Preserve
that private cache across worker restarts; never include it in review artifacts.
See [official authentication documentation](https://learn.chatgpt.com/docs/auth).

## Evidence and decisions

`astra_review.build_packet` freezes the complete accepted manuscript in order,
reject-all text for changed paragraphs, every tracked revision and affected run,
formatting/style maps, all actual comments and anchors, all findings regardless of
status, both verification artifacts, settlement, and available book rules/profile.
Explicit context paths are saved for subsequent validation. A reject-all view is
not the author's original file if earlier preparation made untracked changes.
Unsupported structure, missing evidence, or invalid comment anchors blocks review.

Subscription review assigns every paragraph, revision, comment, finding, issue,
and supplemental record to a bounded Astra request. Saved receipts prove complete
coverage. Shared notes and access to the frozen full packet support cross-book
investigation. A final Astra adjudicator reconciles those reviews and determines
`ready` or `needs_human`; chunk results cannot independently set that verdict.
Coverage describes the complete recorded review, not one call reading every word.

Every actual comment and verification issue gets an explicit disposition.
Revision and finding defaults require affirmed complete coverage, with exceptions
for changes. Nonterminal findings require explicit resolution before `ready`.
Reused issue IDs at different locations remain separate; only exact copied
location/content evidence is deduplicated. Actions must cite known evidence and
match the decision. Tool failures and overlaps remain internal work. Author
questions remain only when the available book context cannot settle the meaning.

## Durability and handoff

The expanded `astra-packet.json` and `astra-evidence/source.docx` preserve the
reviewed snapshot. `astra-review.json` records status, transport, decisions, and
coverage. Subscription plans and chunk receipts live in `astra-subscription/`.
Resume reuses completed matching chunks; changed evidence is rejected. Ambiguous
started requests require recovery rather than silent regeneration or API fallback.
Authentication failures before submission can retry after sign-in. A recorded
exited operational failure can be explicitly reset with
`codex_runner.reset_failed_request(work_dir, request_id, reason=...)`; it archives
the failed attempt and authorizes another invocation of the same evidence. A
request still marked running cannot be reset without completion evidence.

API transport uses one Responses create request with SDK retries disabled,
background storage, and truncation disabled. Its response ID is persisted before
GET polling. A saved ID can recover that same response; no saved ID after an
ambiguous submission cannot silently authorize another POST. Refusal, malformed
output, incomplete coverage, and truncation are operational failures.

`ready` with pending actions is not delivery readiness. `astra_reconcile` applies
only supported exact edits/comment operations and proves them by replaying from
the frozen source and comparing every DOCX member. Unsupported or ambiguous
repairs remain blocked. Certification and handoff then run deterministically;
no later Claude phase rejudges Astra or turns delivery failure into human PR.

An `internal_repair` can replace one finding's `explanation` when its finding ID,
paragraph, and complete original explanation match exactly. It cannot rewrite
anchors, status, historical corrected text, or other evidence fields. Chunk-local
finding IDs retain their positional pairing with the frozen finding rows.

The frozen packet and model responses remain immutable. The reconciliation receipt
records the explanation change and verifies all other evidence unchanged. A durable
prepared plan and exclusive application lock allow recovery across document and
metadata writes without generating another review. An unsupported target still
blocks with the action ID and the failed metadata constraint; deleting a completed
Codex request cache is not a recovery step.

## API cost reference

The optional API route performs exact input-token preflight before its review.
Complete input plus output allowance must fit the context and the configured
ceiling, default $25. It never truncates evidence to fit.

[Official Astra pricing](https://developers.openai.com/api/docs/models/gpt-6-astra),
checked September 9, 2026: $10/M input and $50/M output; above 272,000 input tokens,
$20/M input and $75/M output. Context is 1,050,000 tokens; maximum output is 128,000.
API receipts retain usage and `actual_cost_usd` at uncached list rates, which can
exceed cash charges when caching discounts apply. Missing usage stays unavailable.
Subscription receipts identify allowance-based usage and do not invent an API bill.

The corrected Johnson book plus three supplied reports was approximately $18 at
the API ceiling with 32,768 output tokens. That excluded unavailable production
JSON evidence and is not a full production quote or a subscription charge.
