# Fixed Galley proofreading

New mechanical jobs use `--execution-mode fixed`. Python controls the sequence,
chunk boundaries, model assignments, adjudication, verification, and delivery.
There is no supervising Brain. Every reader corrects only clear proofreading
errors: no stylistic polishing, copyediting, smoothing, or rewriting.

Incoming Word revisions use the same accepted-view policy as Galley's prep
intake. Before any model call, Galley preserves the uploaded original and
creates a separate revision-free working baseline under `runs/fixed/intake`.
The receipt binds both file hashes, the accepted text, resolved revision counts
and changed package members to the review and final certificate. Existing
comments and untouched package members are preserved. Rejecting Galley's new
corrections restores this baseline, including the edits the book arrived with.
Unsupported revision types stop before paid reads with a specific intake error.
Clean files keep their existing source identity; no baseline is created.

The agreed sequence is:

1. Preserve and identify the incoming manuscript.
2. Sonnet reads small samples distributed across the manuscript to determine
   whether it is poetry. Poetry follows the existing spelling-only route and
   finishes without the prose stages or requiring a ChatGPT login.
3. Luna creates the Story Sheet through the API.
4. Sonnet and Luna independently run the typed detectors. The full local
   checking pass supplies additional candidates. Opus reviews candidates that
   lack agreement from both readers and adjudicates disagreements.
5. Code collects numerals, times, and spelled-out number expressions with their
   surrounding text. Luna and Sonnet check those extracts against the existing
   Galley number policy; Opus adjudicates disagreements. This dedicated sweep
   replaces the number group in the typed pass.
6. Opus repairs clearly broken sentences while preserving intended meaning.
7. Luna checks meaning preservation and the correctness of proposed repairs
   through the API.
8. Opus and Sol independently sweep every paragraph of the corrected book.
   Sol uses the saved ChatGPT subscription login. Opus settles disagreements.
9. Fable 5.1 sweeps the resulting book and reviews every proposed Galley comment.
10. Astra sweeps the Fable-corrected book and reviews every surviving comment.

The number sweep reads the house policy shipped in
`config/error_types/number_style.yaml` and `galley/house_style.py`, including
contextual exceptions such as already written-out large numbers. A model does
not invent a separate number policy from the Story Sheet.

Model disagreement does not automatically create an author comment. Opus can
apply a supported correction, drop a false alarm, or identify a question that
requires author knowledge. Fable must resolve, remove, combine, or retain each
proposed comment after reading the corrected manuscript. Astra checks the
survivors. Late corrections receive targeted verification of their changed
passages. The original source and author-supplied comments remain preserved.

Whole-book sweeps record coverage of every paragraph, including books that must
be divided into multiple requests. The workflow checkpoints completed work and
reuses saved responses on resume. A fresh run can still yield different model
judgments; fixed orchestration does not promise identical AI output.

Typed coverage must include every owned paragraph exactly once. If a reader
also lists paragraphs supplied as read-only context, Galley removes only those
known context IDs from the working coverage view. Raw responses remain intact,
and context findings cannot become edits in this chunk. Missing owned IDs,
duplicate IDs and unknown IDs still block completion.

New model suggestions need exact quotations in their assigned paragraphs. An
unknown paragraph, absent quotation or invalid occurrence rejects that proposal
as `rejected_no_anchor`, preserving the original response and a source-bound
diagnostic in the review evidence. It creates neither an edit nor an author
comment, and other valid suggestions from the completed read still proceed.
This applies to typed readers, number checks and whole-book readers, including
broken-sentence repair and frontier formatting proposals. Out-of-scope suggestions,
unsafe replacement characters, unsupported title formatting, unusable Opus
corrections and invalid generated author questions are also dropped individually
as `rejected_invalid_proposal`. A rejected check-stage replacement restores the
previous paragraph and removes the disputed formatting. Bad retained-comment
proposals are dropped; valid comments and adjacent corrections continue. The
raw model output stays unchanged, and no extra call is needed just to discard a
bad proposal. Final comment review still rechecks resolutions that may have
depended on rejected edits. This never authorizes fuzzy matching or suppresses
incomplete coverage, invalid local evidence, stale applied edits or final
document-integrity failures.

Coverage is validated inside the durable call layer before an answer is marked
complete. Orchestration supplies an immutable, request-bound `coverage.json`
inventory for typed paragraphs, number sites, full-book reads, focused checks,
adjudications, meaning/correction checks, poetry sections and comment decisions.
The inventory comes from assigned work, not IDs parsed from manuscript prose.
Incomplete terminal answers consume an attempt and retry only that request
within its original allowance and budget. Raw responses and usage remain
preserved. A previously cached, schema-valid incomplete answer can be reconciled
without changing its request identity or repeating other completed reads.
Missing or changed coverage contracts block recovery and certification.
Subscription retries use distinct transport request IDs, so a retry cannot
simply return the same incomplete cached answer. Unknown submissions still
require reconciliation; exhaustion never grants a fresh retry allowance.

The supplied Atmosphere pasted-chat method is preserved with an
[item-level coverage review](galley-press-prompt-coverage.md). Its 120 indexed
rules plus prose and table instructions become 149 accounted-for source items.
Fable and Astra share the extracted editorial brief; Opus adjudication and Luna
checks use the same rules. Narrow typed readers retain their category prompts
plus the shared scope, variant and punctuation guards. The original chat prompt
cannot schedule additional agents, change the existing number policy, make
silent edits, or broaden proofreading into stylistic rewrites.

The Story Sheet records variant/genre evidence, vocabulary choices and intended
tense/person with explicit section exceptions. Fable and Astra receive current
dialogue-matrix, serial-comma, quotation and narrative-tense sites and must
acknowledge every assigned site ID. The local tense profiler is a heuristic,
not authority to rewrite a deliberately present-tense chapter. Applicable
citation passages, real note locations and conservative formatting evidence
support the final reads. Confirmed roman long-work titles can be proposed as
tracked italics and pass through the correction gate. The final report records
actual final pattern counts, coverage and limitations, not assumed zeroes.

Local checks cover spelling and near-miss words, all house punctuation sweeps,
heading capitalization and vocabulary, quotation balance, name and spelling
consistency, abbreviations and accents, explicit date/weekday mismatches,
and the local LanguageTool grammar rules. The existing candidate generators
also examine commas, homophones, lists, heading sequence, repeated words, and
term consistency. Number and currency checks remain assigned to the bespoke
number stage so they do not receive a second independent number policy.

Quote and space normalization and possible speaker boundaries are recorded as
proposals; scanning never reformats the incoming manuscript. Citation-pattern
consistency and a deterministic structure extract supply further evidence.
Fable and Astra receive the current opening/heading excerpt when reading
structural or opening passages. This excerpt is explicitly incomplete and
cannot establish that a contents entry is missing; only supported wording or
numbering mismatches qualify for proofreading review.
Anachronism checks require an explicitly stated era. Reading-level and word-echo
measurements stay in internal diagnostics and cannot justify edits or author
questions. Poetry stays on its spelling-only route, including poetry sections
excluded from the prose checking pass.

After the main prose repairs, a bounded local completion pass checks residual
house-rule errors and other occurrences of accepted word corrections. These
are fresh proposals for Opus, followed by the usual correction checks. Local
rules never authorize an edit on their own. Their candidates pass the same
proofreading and intent guards as the model readers; Fable and Astra still
review any resulting comments.

Local scan receipts record paragraph coverage, configuration, implementation
hashes, and results. Successful scans are reused on resume. A missing checker,
incomplete LanguageTool response, or failed scan blocks completion rather than
being counted as a clean paragraph. Final native-document checks still require
that rejecting tracked edits restores the source, accepting them matches the
clean manuscript, and the delivered files match their certified evidence.

A failed local scan stores its complete normalized request. After a code repair,
resume may rerun that unpublished scan when only implementation hashes changed.
The failure and marker transition remain recorded, and every local check runs
again. Completed packets are never relabeled or replaced; changes to source,
configuration, runtime assets or prepared findings still require a fresh run.
Earlier model responses and their original budgets remain intact. An old failure
without a complete request cannot take this automatic recovery path.

Fixed driver checkpoints report `running` until certification and packaging
finish. A startup checkpoint is never proof that a manuscript is complete.

The hosted image includes Java and a pinned LanguageTool 6.8 distribution,
installed and smoke-tested during the image build. It uses the
[Python wrapper maintainer's verified build](https://github.com/jxmorris12/language_tool_python/releases/tag/LanguageTool-6.8)
of the official LanguageTool 6.8 source, with `language_tool_python==3.4.0`.
The archive SHA-256 is
`6a7f6b67b779ae9505f7579f0c41453ea8d1bd72ae750bdc2c55ba974281467d`.
Galley also verifies a pinned inventory of every extracted file before starting
the server. The image stores it at `/opt/languagetool/LanguageTool-6.8`, outside
the writable manuscript volume. The checker runs on loopback, ignores HTTP
proxy settings, and has no runtime download or public grammar-service fallback.

For a local installation, install the `galley` and `languagetool` extras and
[Java 17 or newer](https://dev.languagetool.org/java-api). Download that exact
archive explicitly, then install it into a directory you own:

```sh
python -m galley.local_runtime install --archive /path/to/LanguageTool-6.8.zip \
  --directory /path/to/LanguageTool-6.8
export GALLEY_LANGUAGETOOL_HOME=/path/to/LanguageTool-6.8
python -m galley.local_runtime verify
python -m galley.local_runtime smoke
```

`verify` checks files and Java without starting a server. `smoke` starts only
the local server and checks a synthetic sentence; it makes no model call and
reads no manuscript. The installer validates the archive before writing files.
Replace an altered installation explicitly rather than allowing a book run to
repair or download its dependencies.

Preview a new job without calling models:

```sh
docproof galley drive --book "Author - Book 1.docx" --slug author \
  --execution-mode fixed --dry-run --json
```

Run it by removing `--dry-run`. Resume with the same book and workspace; the
fixed runner resumes its own checkpoints. `--from` and `--phases` are legacy
options and cannot skip fixed stages. Model and effort overrides are rejected
because they would change the prescribed recipe. `--approve auto` accepts the
fixed recipe within the supplied budget; inspect it with `--dry-run` first when
needed. The preview names the actual stages and readers instead of listing
supervising Brain models.

Astra uses the saved ChatGPT subscription in fixed mode. The legacy
`--astra-transport api`, Astra budget, chunk-size, and output-size overrides
are rejected for this recipe. The supplied API budget covers the API readers;
saved usage and remaining engineering limits are recorded in the driver ledger
and worker progress.

The fixed call ceilings are 10,000 attempts and 20,000,000 output tokens, with
the API spending ceiling supplied by `--budget` (default $10). These are durable
engineering limits, not estimates or claims about subscription capacity.
Completed calls are reused; unknown usage retains its reserved allowance.
Nondefault legacy `--review-rounds`, `--review-calls`, and
`--review-output-tokens` overrides are rejected because they configure the
earlier verification and settlement loop.

Existing workspaces retain their saved `code` or `session` mode. Unversioned
workspaces with legacy progress stay on the legacy workflow. A fixed job cannot
be selected halfway through a legacy manuscript; use a separate workspace for a
fresh run. Fixed checkpoint identity also survives an interruption before the
first driver result is written.

The compulsory local checks belong to fixed recipe version 2. A workspace
created by fixed version 1 cannot resume under the new recipe: start a fresh
workspace so the added checks cover the original manuscript and become part
of its certification. Existing checkpoints are not silently relabeled as having
completed checks that were absent from their recipe.

The fixed sequence removes supervisory sessions and duplicate number checking.
Total time, usage, comment counts, and proofreading quality still need a
same-manuscript comparison before claiming a measured efficiency gain.
