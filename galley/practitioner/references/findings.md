## Channels (the demotion trap)

`general_error` is **query-channel** — replayed EDIT rows on it silently become
comments. Curated/replayed edits must ride a custom EDIT-channel type
(`curated_fix`-style) declared in `error_types`, listed FIRST so composites win
their spans.

## File contracts (so you never guess a schema by trial and error)

**Paragraph ids** (`body-NNNN`) never map cleanly onto your own text
extraction (textboxes/tables shift the numbering). Get them from the rack:
`docproof inventory IN --para-map` prints `para_id<TAB>length<TAB>canonical
text` for every paragraph — the exact ids and post-normalization text that
import rows and intent-zone regexes must match. Note the canonical text
STRIPS paragraph-trailing whitespace; an original_text ending in a space will
not anchor.

**Intent-zones file** (`intent_zones_file`, `galley intent-zones --zones`):
JSON, either a bare list of zones or `{"zones": [...]}`. Each zone:

```json
{"label": "email_headers", "permission": "locked",
 "para_ids": [], "para_range": [], "terms": [], "regex": "^(To|From|Subject):.*$",
 "quotes": false}
```

`permission`: `locked` | `punctuation` | `open`. Selectors union; `quotes` is a
BOOLEAN (protect quoted spans), not a list. Unknown keys are rejected loudly —
if your file resolves zero spans, your regex matched nothing, not the schema.
Only the span the selector MATCHES is protected — anchor a regex over the whole
line (`.*$`) when the whole line is the zone.

**import-findings / replay rows**: JSON array (or `{"findings": [...]}`). Each
row: `para_id`, `original_text` (must anchor VERBATIM in the canonical
paragraph), `corrected_text`, optional `error_type`, `explanation`/`comment`,
`occurrence`, `confidence`. A row with `queried`/`force_query` true — or whose
`error_type` is a shipped query-channel type (`general_error` included) — rides
as a margin comment, never as an edit; `original_text == corrected_text` plus a
comment is a pure author question. Rows on a shipped format-channel type
(`title_italics`) replay as italic marks (`corrected_text` = the span inside
`original_text`). Multi-fix rows are fine: the validator splits each row into
minimal per-touch tracked changes, so a full-paragraph O→C with three fixes
lands as three small marks, not a block replace. The edit guard does not apply
on this path (rows are curated); the word-count delta guard still does.

## Filing and replay discipline

Use `docproof inventory IN --para-map > runs/paragraph-map.txt` and inspect
only the paragraphs needed; do not load the map or book wholesale. A paragraph's
id includes tables/textboxes: never infer it from your own extraction order.

Before filing a one-off catch, search the complete source for the surface AND
its inflections/construction family. File every confirmed site explicitly;
curated/imported rows do not seed recurrence propagation. Rule-shaped catches
become approved bespoke sweeps. Your pen writes findings; the engine writes Word.

For accepted-view quotes use `import-findings --anchor accepted --run RUN`;
for post-sweep quotes use `--after-sweeps`. Let the edit map re-anchor them rather
than hand-building micro-spans. Pure insertions use serialized anchors. Keep
lane, cluster, silent, and withheld attribution through replay.

One owner per span; composites and repair clusters are atomic. A residual inside
an owned span goes through settlement, never a hand-patched replacement. Dedupe
same-point insertions and whole clusters, not individual split members. Reject
editorial notes in replacement text. Corrected text uses curly quotes; avoid a
whole-paragraph fake diff caused by sanitizing only one view. Rebuild from curated
rows with new detection off under `final-replay`; do not re-fire recurrence,
residuals, consistency, or unclosed-quote queries on already corrected text.

A strict sentence screen, artifact scan, reject-all round trip, and independent
verification apply to imported findings just as they do to paid detector rows.
