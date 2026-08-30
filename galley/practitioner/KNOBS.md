# KNOBS — the rack's control surface, distilled

This file exists so you **never `cat` `config.py`, `default.yaml`, `--help`, or
`sweeps.py` into your context.** Those reads are 60–90k characters that then
ride your context every turn for the rest of the run — the single biggest
cache-read cost we've measured. Everything you need to write a run config is
here. If a knob you need genuinely isn't documented here, that is an
ESCALATION (the knob may not exist), not a reason to go read the source.

## What changed in v0.143.0 (tense/cites verbs + serial-comma guard)

- **`docproof tense IN --config C`** — new $0 report-only verb: whole-book
  narrative-tense profile (baseline tense + person, per-paragraph verdicts
  with dialogue stripped, contiguous against-the-baseline runs, headline
  present share). Run it at profile time on EVERY book; the runs are the
  read-first list for a planned conversion read. `--json` for the
  machine-readable profile. No config section — it is a verb, not a lane.
- **`docproof cites IN --config C`** — new $0 report-only verb
  (nonfiction/academic): author-date citations vs the reference list both
  ways, chapter/figure/table cross-refs vs the book's own headings and
  captions. Auto-skips checks whose scaffolding the book lacks. Raise queries
  from the report yourself; nothing auto-applies. `--json` available.
- **`serial_comma` (type v2) no longer proposes a comma on polysyndeton** —
  an "A and B and C" chain with no commas is the author's rhythm (35 of 38
  such flags were wrong on Paik 2); the rule now applies only where the
  author is already separating items with commas.

## What changed in v0.141–0.142 (Purpura head-proofreader review)

- **`sweep_decade_apostrophe`** joined the sweeps (the 80s → the ’80s;
  the/early/late/mid leads only — ages and temperature ranges left alone).
  The paste-ready block below already includes it.
- **`toccheck:`** is a new top-level section — the contents-vs-body structure
  read (Luna over a small extract, ~pennies, ON by default, query-only).
- **`consistency.time_style`** (on) queries bare clock hours in a book that
  writes 11:00-style times; **`consistency.accent_loanwords`** (on) queries
  Si→Sí-class bare loanwords.
- **`style.heading_vocab_queries`** (on) queries AFTERWARD/FOREWARD headings.
- **The validator demotes pure space-deletion merges to queries** ("blood
  work"→"bloodwork" never auto-applies — compound styling is the author's
  call). A replayed row whose whole effect is deleting one inter-word space
  rides as a comment; don't fight it, hand-fix if the merge is truly wanted.
- **title_italics never touches TV shows** (house sets them in quotes), and
  number_style leaves stage/type/grade labels and bare clock hours alone.

## What changed in v0.133.0 (Purpura beta round 2)

- `docproof capabilities` now carries **per-verb arg schemas** (flags,
  required, choices) — you never need `--help` for a signature.
- `--stage final-replay` now zeroes **every** detector (residuals, spellcheck,
  consistency, glossary, LT, gates) — "detect nothing new" is real.
- **Query comments collapse one-per-TYPE** past `comment_collapse` (default 3):
  a number-policy class or a consistency family leaves ONE counted comment;
  the report lists every site. Below the threshold a query is its own question.
- **Intent zones now guard every channel at finish()** — model edits, repair,
  and replayed/imported rows are downgraded inside protected spans, not just
  sweeps; zero-width insertions at a zone boundary count as inside.
- `docproof replay` round-trips pure insertions (empty original_text) via the
  row's serialized anchor.
- certify gained a **spellfix sanity** check (truncated-stem signature).
- A materialized config may write a top-level list at COLUMN 0 under its key
  (`sweeps:` then `- sweep_x` unindented) — valid YAML; don't misread it as an
  empty section.

## The one mechanic that bites

A run config **REPLACES `default.yaml` wholesale** — it is not a patch. Any
top-level section you omit reverts to code defaults. Two omissions silently
gut a run and look like a config that "just uses defaults":

- Omitting **`error_types:`** zeroes **every typed LLM pass** — the log prints
  `0 error type(s) … in 0 pass(es)`. The ensemble runs *through* those passes,
  so a defined `ensemble:` block is **inert** with no `error_types`; the run
  catches only sweeps + spellscan + LT-basic and misses every correctly-spelled
  homophone/grammar error (they're/their, effected/affected, then/than,
  who/whom). This one bit a seeded benchmark (Lighthouse, 2026-08-26).
- Omitting **`sweeps:`** turns **every** sweep off.

Restate every section you touch, **and restate `error_types:` and `sweeps:`
even when you think you want defaults.** You cannot read default.yaml (context
discipline), so the full shipped lists are embedded below — paste them in
verbatim. Never assume "leave it out = use defaults"; leave it out = OFF.

`--profile`, `--stage`, and genre packs are post-load overlays (they layer on
top). `error_type_override_dir` shadows shipped detector prompts by key.

## Stages, genres, and the approval gate (post-load overlays)

Three separate axes compose onto a base config. Precedence, strict-to-loose:
**profile > stage > genre > base.**

- **`--stage`** (`config/stages/`) chooses *which lanes run* and LOCKS some so a
  genre cannot reopen them. `mechanical-wave` is the portable Wave 1 recipe:
  the ensemble block below, over the base's full typed passes/sweeps, repair on,
  and the copy-edit lane (smoothing edits, rewrite) **locked off**.
  `copyedit-wave` runs style on already-proofread text; `external-judgment`
  proposes for the packet route; `final-replay` zeroes detection
  (`error_types: []`, ensemble off) to rebuild from accepted decisions.
- **`--genre`** (`config/genres/`) sets *posture only* (judge stance, name bar,
  smoothing volume, query scans) — never a lane switch. Taxonomy: `general_
  fiction`, `literary_memoir`, `fantasy_sf`, `general_nonfiction`, `academic`,
  `historical`, `religious`, `self_help_business`. Run theological non-fiction
  under `religious`, never `self_help_business` (that one turns edits + rewrite
  on).
- **`docproof galley approve`** freezes the composed config into `approval.json`
  (source + config hashes, allowed models/providers, stage, lanes, budget).
  `docproof review --approval …` refuses to run on any deviation; `docproof
  galley certify` is the delivery gate. `docproof galley routes` prints the
  effective model→provider egress map — the one place routing is legible.

Compose all three into a reviewable file:
`docproof galley genre-pack religious --stage mechanical-wave --out runs/book/mech.yaml`.
A materialized config resolves its `error_types/` from the packaged prompts when
no sibling dir exists, so it is self-contained wherever it lives.

**The ensemble recall recipe (what `mechanical-wave` bakes in):**

```yaml
ensemble:
  detectors:
    - {model: gpt-5.6-luna, effort: low}
    - {model: claude-haiku-4-5, effort: low}   # diverse union = recall
  verifier_model: gpt-5.6-luna                  # precision over the disputed set
  verifier_effort: high
  verify_policy: disputed
```

`ensemble` fires ONLY through `error_types` — pair it with the full typed-pass
list, never alone (see the trap above).

## Paste-ready shipped defaults (you cannot read default.yaml — copy from here)

These are the exact shipped lists as of v0.131.0. If a config wants the typed
passes or the built-in sweeps, paste these blocks in **whole**. The ensemble
fires **only through `error_types`** — no `error_types`, no ensemble, no typed
recall (log reads `0 error type(s) in 0 pass(es)`).

```yaml
# The typed LLM passes — the recall engine. Groups = one read each; keep all 9.
error_types:
  - [repeated_word, spelling, homophone_confusion, apostrophe_error, capitalization]
  - [serial_comma, complex_list_semicolon, introductory_comma,
     direct_address_comma, tag_question_comma]
  - [dialogue_tag, speaker_change]
  - [number_style, currency_style, ly_adverb_hyphen, title_italics]
  - [comma_splice, run_on_sentence, compound_sentence_comma,
     subject_verb_agreement, that_which, that_who]
  - [tense_shift, pronoun_agreement, missing_word, preposition_error]
  - [try_and, list_intro_colon]
  - terminal_mark        # query-channel: asks, never edits
  - unnecessary_comma    # the one comma rule that DELETES; isolated on purpose

# The 16 deterministic $0 sweeps. Omitting the section turns ALL of them off.
sweeps:
  - sweep_ellipsis
  - sweep_dash
  - sweep_stacked_punctuation
  - sweep_doubled_word
  - sweep_century
  - sweep_compound_number
  - sweep_dialogue_tag
  - sweep_terminal_period
  - sweep_quote_punctuation
  - sweep_nested_quote
  - sweep_time_of_day
  - sweep_deity_capital
  - sweep_dialogue_splice
  - sweep_initialism
  - sweep_decade_apostrophe
  - sweep_trailing_space
```

To ADD a per-category repeat read or a bespoke sweep, paste the block above and
append/modify — don't hand-pick a subset unless you mean to drop the rest.
**KNOBS.md is the authority: if a knob is listed here it exists — trust it over
a source `grep`.** (`languagetool.picky` is real — config.py:679 — and was once
wrongly dropped on a bad grep.)

## Top-level sections (names are exact)

`api · chunking · skip · normalize · speaker_split · spellcheck · consistency · glossary ·`
`factcheck · toccheck · genre_scans · flights · continuity · chapter_continuity · rounds ·`
`adjudicate · rewrite · languagetool · sapling · chapter_sweep · repair ·`
`smoothing · low_confidence · meaning_check · fix_check · error_types · style ·`
`edit_guard · sweeps · residuals · recurrence · candidate_screening · ensemble ·`
`tracked_changes_policy · min_confidence`

## The knobs you actually turn (with defaults + the lever they move)

| Knob | Default | What it does / when to change |
|---|---|---|
| `min_confidence` | `medium` | low\|medium\|high gate for APPLYING a change. |
| `speaker_split.enabled` | on | Splits "…go.” “You…" two-speaker paragraphs as a TRACKED paragraph break + declarative comment, in prepare (whole-doc runs only). Judgment cases stay with the speaker_change query. |
| `consistency.variant_policy` | `off` | "us" queries consistently-British spellings (theatre→theater), one per word, regular families only. House policy call. |
| `consistency.deity_pronouns` | on | Query lowercase he/his/him in deity-anchored sentences when the book capitalizes reverent pronouns (self-gating). |
| `consistency.time_style` | on | Query bare clock hours ("around 4") in a book that writes 11:00-style times. Self-gating on ≥3 H:MM times (`time_min_with_minutes`). |
| `consistency.accent_loanwords` | on | Query bare loanwords the dictionary accents (Si→Sí, senor→señor); curated table, one query per word, protected names skipped. |
| `toccheck.enabled` | on | Contents-vs-body read (Luna over a small structure extract, ~pennies): entry wording vs the chapter page, part/chapter numbering, listed-but-missing entries. Query-only, cached per draft. |
| `style.heading_vocab_queries` | on | Query AFTERWARD/FOREWARD-class headings (standard label is AFTERWORD/FOREWORD). |
| `ensemble` | off | Luna+Haiku union + a verifier — the recall wave-1 recipe. Ready-made as `--stage mechanical-wave` (see the ensemble block above); fires ONLY through `error_types`. |
| `error_types` | full shipped list | The typed LLM passes. **OMITTING THE SECTION ZEROES EVERY PASS** and makes any `ensemble:` inert (`0 error type(s) in 0 pass(es)`). Restate the full default list to keep them; the ensemble only fires through these. |
| `error_types[key]` | `{group,passes,token_budget}` | Per-category repeat reads. `passes:2` = union re-read (house-comma recipe); costs ~2× that category. Custom EDIT-channel replay types go here (see below). |
| `languagetool.picky` | off | +~1 candidate/44k words; most picky rules are the filtered style class. |
| `languagetool.edit_word_replacements` | off | Whether an LT suggestion that swaps one WORD for another (boop→book, sesh→mesh) may become a tracked edit. OFF: every real-word swap routes to a margin QUERY instead — LT still edits punctuation/spacing/casing/en-dash blind, still ASKS about a word change, but never applies one silently (Purpura: 35 auto-applied corruptions). LT also never de-accents a word MW spells with the accent (cliché), and now respects the protected-noun allowlist for EVERY rule, not just misspellings. Turn on only for a house that has measured LT word edits safe. |
| `sweeps` | 15 rules | Deterministic $0 rules. OMITTING THE SECTION KILLS ALL OF THEM. Add bespoke `.py`/regex keys here. |
| `smoothing.max_per_1000_words` | preset (5.0 business) | Copy-edit volume ceiling. Drop toward 2.5 for voice-heavy authors (fragments/profanity/initial conjunctions are the product). |
| `residuals.max_per_rule` | 150 | Cap on same-rule findings before overflow is DROPPED. Raise (e.g. 300) when a legit high-count class (numerals) would overflow. |
| `edit_guard.max_added_chars` | 16 | Max chars an edit may ADD before it must ride as a query. Raise ONLY for hand-verified composites. |
| `genre_scans.reading_level` | on w/ pack | Turn off when it's noise for the genre. |
| `low_confidence` (`confirm`) | on | Recovers stranded low-conf edits (compound_sentence_comma / comma_splice in dialogue). |
| `repair` | trigger ≥3 flags/sentence | Broken sentences fixed as atomic clusters via Fable. |
| `meaning_check` / `fix_check` | off in base; ON via `--stage mechanical-wave` | The two gates. The BASE config ships them off; the mechanical-wave stage turns both on (that stage IS the "both gates" recipe). `meaning` punctuation/case-only diffs bypass to `fix` only (deterministic) — do not disable to "save a hold". |
| `chapter_sweep` | off | ~$18 Fable / ~$3 Sonnet, or $0 as subagents. Plan line, never default. |
| `sapling` | off | ~$34/long novel, key is FLY-ONLY (no-ops silently local). Explicit char budget or leave off. |
| `tracked_changes_policy` | `abort` | `accept_all_first` to review a doc that already has tracked changes. |
| `recurrence` | on | Propagates a fix to identical surfaces — degenerate-surface flood guard is in ≥v0.116. |

## The judgment subagent — a $0 Opus/Fable read for whole-book judgment

The Purpura head-proofreader comparison drew the line precisely: the rack's
chunked passes win the mechanical tail (deity caps, that→who, tense slips —
all "Claude missed this"), and a single frontier context wins the JUDGMENT
tail (a "lower" that should be "higher", a sentence repeated from earlier in
the paragraph, wording that drifted between two copies of itself). A frontier
model degrades as you widen its mandate over a long, error-dense book — so
the recipe is a **session subagent with a deliberately narrow mandate**, per
the model doctrine (Claude subagents never bill; Opus for the difficult read,
Fable for long-horizon threads; never Haiku here).

**Mandate (verbatim, keep it this narrow).** The subagent hunts ONLY:
- **wrong-direction words** — a word whose opposite is plainly meant
  ("bilirubin levels will be much *lower*" the morning after a decline);
- **in-scene sense breaks** a grammar pass can't see (an action or claim the
  surrounding paragraph contradicts);
- **self-repetition** — a sentence or clause repeating from earlier on the
  page with no rhetorical purpose;
- **wording drift between two copies of one line** (an epigraph, a refrain, a
  quoted callback) — beyond what `toccheck` already covers for the contents.

It does NOT flag spelling, punctuation, numbers, style, or anything a typed
pass owns — every mechanical catch it reports is noise that erodes the audit.

**Shape.** Chapter-scoped windows (the chapter_sweep window discipline: ~24k
chars, incremental output), each prompt carrying a ~1-page rolling casefile of
whole-book facts (names, established directions/quantities, refrains seen) —
never the whole book in one context. One Opus subagent per window is the
default; use Fable when a thread genuinely spans the book. Require each
subagent to WRITE its rows to a file (subagents that answer inline lose work).

**Output contract.** Verbatim quote→correction rows, `import-findings`
schema, on the **EDIT-channel `galley_read` type** (declared in
`error_types`, listed first) when the fix is decidable — a wrong-direction
word is mechanics, apply it tracked. A genuine author-knowledge question
rides a query row instead, under the queries-last-resort doctrine (decide or
stay silent first; collapse families). Same adjudication, artifact scan, and
reject-all audit as every other lane — this is the "Your own pen" path run
as a fleet, not a new channel.

**Plan line.** $0 (session subagents), but it is still a plan line with an
expected-yield note, and its rows are attributed to `galley_read` in the
ledger so the audit can see what the judgment lane contributed. Redding
calibration: chapter reads of this shape found ~57 real fixes the rack
missed; Purpura's judgment misses (lower/higher, the repeated sentence) are
the acceptance test — a run of this lane that would not have caught those
two is mis-prompted.

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

## Bespoke sweep contract (so you never read `sweeps.py`)

- Author a `.py` sweep or a yaml regex; run `docproof sweep IN --rule F`
  **dry-run first** — it prints the POST-NORMALIZATION canonical text your rule
  must target (quotes already curled, canonical spaces). Then `--apply`.
- Must be **idempotent** — running twice changes nothing the second time; the
  verb refuses non-idempotent rules.
- Sweeps **claim spans first**: a curated edit whose span contains an ellipsis
  loses to `sweep_ellipsis` — target only characters outside the claim.
- Same-point insertions from two sources compose into `,,` — dedupe by
  insertion POINT, then iterate the artifact scan until clean.
