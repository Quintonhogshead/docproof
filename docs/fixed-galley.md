# Fixed Galley proofreading

New mechanical jobs use `--execution-mode fixed`. Python controls the sequence,
chunk boundaries, model assignments, adjudication, verification, and delivery.
There is no supervising Brain. Every reader corrects only clear proofreading
errors: no stylistic polishing, copyediting, smoothing, or rewriting.

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
