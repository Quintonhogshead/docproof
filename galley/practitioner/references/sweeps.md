## Bespoke sweep contract (so you never read `sweeps.py`)

- A bespoke rule is NOT a `sweeps:` key: Config accepts only the built-in
  keys and rejects a path or a custom name ("unknown sweep"). Bespoke rules
  live outside the run config and enter the build as import rows.
- Author a `.py` sweep or a yaml regex; run `docproof sweep IN --rule F`
  **dry-run first** — it prints the POST-NORMALIZATION canonical text your rule
  must target (quotes already curled, canonical spaces) and the match count.
- **Fold the matches in as `import-findings` rows** (the Georgis path,
  2026-09-04), rather than `--apply`-ing a separate build: `docproof sweep IN
  --rule F --json > runs/sweep_<key>.json` gives one finding per match; write
  one import row per match with `para_id`, `original_text` = a ~12-character
  window around the match (the matched text plus a few characters either
  side — NOT the whole sentence, so the row cannot collide with another
  row's claim on the same sentence), `corrected_text` = that window with the
  fix applied, `occurrence` = which occurrence of that window in the
  paragraph (count from the canonical text; a 12-char window repeats more
  often than a sentence does), `error_type` = the rule key (declare it in
  `error_types` or let intake remap it onto `imported_edit`), `confidence:
  high`. Then `docproof import-findings ROWS IN --config C --out RUN` with
  the rest of the curated rows. `--apply` remains for a one-off standalone
  build only.
- Must be **idempotent** — running twice changes nothing the second time; the
  verb refuses non-idempotent rules.
- Sweeps **claim spans first**: a curated edit whose span contains an ellipsis
  loses to `sweep_ellipsis` — target only characters outside the claim.
- Same-point insertions from two sources compose into `,,` — dedupe by
  insertion POINT, then iterate the artifact scan until clean.

## Scope and canonical targets

Use only PLAN.md's approved sweeps, author their files in one batch, dry-run each,
and read counts plus a few before/after samples. A count beyond the approved blast
radius is a scope change; do not silently expand it. Preserve intent zones and
read `references/house-rules.md` before choosing replacements.

Normalization (curling quotes, canonical spacing) defines analysis coordinates;
it is not a silent edit to the delivered wording. Each wording change needs a
tracked finding. `sweep_terminal_period` skips attribution/display/template lines,
and must be OFF on split-paragraph books where it would punctuate fragments.
Dialogue tags followed by an opening quote do not acquire a period.
`sweep_decade_apostrophe` applies to the/early/late/mid decade leads, never ages
or temperatures. Skip headings structurally (short line, no terminal punctuation,
or reviewable=false), not by Word style or a book-specific regex.
