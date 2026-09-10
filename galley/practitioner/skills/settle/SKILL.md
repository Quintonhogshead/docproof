---
name: settle
description: Close every open verification item through the engine, preserving internal repairs and the recorded editorial outcome.
---

# /settle — close candidates without forwarding internal work

Read `references/house-rules.md` and `references/comment-reconciliation.md` once.
All paths are workspace-relative. Load `references/settlement-contracts.md` only
for exact artifact/owner-map fields; use `references/findings.md` only when an
import/replay contract is needed. Do not read old release notes or full KNOBS.

## Preconditions and exact driver policy

Verify must have current, complete evidence for the actual build. Use that build's
$0 replay config, from the workspace root so intent-zone paths resolve; never
rewrite the paid config frozen in approval. Every applied edit and accepted
paragraph still requires the planned independent two-pass verification.

The driver supplies exact flags: by default
`--until-clean --rounds 3 --quiet-floor 4 --quiet-share 0`.
At most three rounds; new items <= 4 is quiet; the percentage rule is off.
Use explicit run overrides exactly. Standalone CLI defaults (quiet floor 3,
quiet share 0.02) are different; do not substitute them for the driver flags.
A cap stops work without proving it complete. Preserve internal repairs and all
nonconvergence evidence rather than relabeling it as author questions.

## Procedure

1. Inspect bounded counts from `docproof galley residuals RUN --source BOOK
   --config C`; read only the needed residuals and their owner resolution.
2. Run `docproof galley settle RUN --source BOOK --config C --engine subagent
   --approval approval.json` with the phase's exact settlement flags, output
   redirected to runs/settle.log. Run in the FOREGROUND and wait for exit.
3. The engine translates accepted-view residuals through editmap.json into source
   spans, then drops disproved/duplicate/voice/intent-zone findings, absorbs a
   correction into its existing owner as one composite, adds a correction on
   untouched text, asks a specific author-knowledge question, or keeps an
   unresolved repair internal. The narrow judge must identify a mechanical
   category and preserve meaning. Chapter/part labels and equivalent number/time
   formatting are mechanics; actual value changes are facts.
4. Dictionary-known closed compounds and accidental verbatim repeated passages
   can be edits; duplicate questions are drops. No dictionary, failed anchors,
   editorial notes, exhausted rounds, or a tool error alone justify an author
   comment. Distinct genuine questions in one sentence stay distinct. Preserve
   every independent repair even when several changes touch the same paragraph.
5. The engine rebuilds at $0 and independently verifies touched paragraphs.
   A composite verifier flag gets the guarded second look; failed/reverted
   corrections remain evidenced and cannot be silently shipped. Rebase retries
   through the current edit map; never hand-patch an owner's replacement.
   Planned-result mismatches, missing edits, and introduced duplication restore
   verified text and leave failed repairs internal. Normal word-shaped settlement
   fixes may propagate to identical sites in the same/neighboring paragraphs;
   curated/imported one-offs do not seed book-wide propagation.
6. Inspect settlement.json counts, notes, and open; record every unresolved item
   and outcome honestly. `galley state . --advance settled --results RUN
   --source BOOK --config C` refuses while open work remains. Never fake the
   state or delete a repair to make it advance.

Independent `galley verify --out` passes automatically register their artifacts
with the reviewed run. Settlement reads all registered passes, including the
slow type-and-compare pass; keep their files and original verification hashes.
After rebuilding, repeat both required verification passes on the new build.

When independent reads establish missing author knowledge but the narrow judge
keeps returning `no_suggestion`, record the actual editorial ruling through
`--queries runs/settle-queries.json`. The file is
`{"build_sha256":"CURRENT_DELIVERABLE_SHA256","queries":[{"residual_id":"r-...",
"para_id":"body-...","quote":"EXACT_RESIDUAL_QUOTE","question":"Specific author question",
"missing_knowledge":"What fact or intended wording only the author knows"}]}`.
Use an open residual from `galley residuals RUN --json`, the current deliverable
hash, and evidence already recorded in decisions.md. Missing engines, unanchored
text, locked source features, or spent rounds are not author knowledge. The
engine checks the build and residual evidence; it writes the query and its
settlement record without changing the approved config. Never hand-patch the
findings or settlement ledger. Source-restoring corrections now withdraw their
owning edits through settlement instead of producing rejected no-op rows.

## Queries and outcome

Compare actual questions with approval's comment_budget before certification.
Collapse same-rule families and decide anything the book answers; never raise
its ceiling. Each surviving question is one specific question plus one sentence
of evidence. Reconcile comments against final text, preserving original author
comments and requiring a fresh hash-bound reconciliation after a document change.

Enrolled (`astra-review-required.json`) workspaces leave the final editorial
verdict to the driver's Astra review. Preserve a valid tracked snapshot and all
Verify/settlement evidence; never overrule Astra, turn a technical error into
needs_human, or resume an extra Claude review after its final decision. Open
internal repairs still block certification. Without enrollment the legacy driver
records nonconvergence/outcome and its best-available handoff; an incomplete or
stopped handoff is not a certified completion.

Keep the approved subscription model/effort. The driver explicitly uses
`--engine subagent`; never replace it with an automatic API fallback or a cheaper
unapproved model. Every final finding must be applied, dropped with a reason,
or a justified author query; otherwise the internal work remains open.
