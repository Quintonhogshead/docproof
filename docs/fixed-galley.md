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
4. Sonnet and Luna independently run the typed detectors. Opus adjudicates
   disagreements.
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

The fixed sequence removes supervisory sessions and duplicate number checking.
Total time, usage, comment counts, and proofreading quality still need a
same-manuscript comparison before claiming a measured efficiency gain.
