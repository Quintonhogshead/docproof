# KNOBS — the rack's control surface, distilled

This file exists so you **never `cat` `config.py`, `default.yaml`, `--help`, or
`sweeps.py` into your context.** Those reads are 60–90k characters that then
ride your context every turn for the rest of the run — the single biggest
cache-read cost we've measured. Everything you need to write a run config is
here. If a knob you need genuinely isn't documented here, that is an
ESCALATION (the knob may not exist), not a reason to go read the source.

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

`--profile` and genre packs are post-load overlays (they layer on top).
`error_type_override_dir` shadows shipped detector prompts by key.

## Paste-ready shipped defaults (you cannot read default.yaml — copy from here)

These are the exact shipped lists as of v0.125.0. If a config wants the typed
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

# The 13 deterministic $0 sweeps. Omitting the section turns ALL of them off.
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
```

To ADD a per-category repeat read or a bespoke sweep, paste the block above and
append/modify — don't hand-pick a subset unless you mean to drop the rest.
**KNOBS.md is the authority: if a knob is listed here it exists — trust it over
a source `grep`.** (`languagetool.picky` is real — config.py:679 — and was once
wrongly dropped on a bad grep.)

## Top-level sections (names are exact)

`api · chunking · skip · normalize · spellcheck · consistency · glossary ·`
`factcheck · genre_scans · flights · continuity · chapter_continuity · rounds ·`
`adjudicate · rewrite · languagetool · sapling · chapter_sweep · repair ·`
`smoothing · low_confidence · meaning_check · fix_check · error_types · style ·`
`edit_guard · sweeps · residuals · recurrence · candidate_screening · ensemble ·`
`tracked_changes_policy · min_confidence`

## The knobs you actually turn (with defaults + the lever they move)

| Knob | Default | What it does / when to change |
|---|---|---|
| `min_confidence` | `medium` | low\|medium\|high gate for APPLYING a change. |
| `ensemble` | off | Luna+Haiku union + a verifier — the recall wave-1 recipe. |
| `error_types` | full shipped list | The typed LLM passes. **OMITTING THE SECTION ZEROES EVERY PASS** and makes any `ensemble:` inert (`0 error type(s) in 0 pass(es)`). Restate the full default list to keep them; the ensemble only fires through these. |
| `error_types[key]` | `{group,passes,token_budget}` | Per-category repeat reads. `passes:2` = union re-read (house-comma recipe); costs ~2× that category. Custom EDIT-channel replay types go here (see below). |
| `languagetool.picky` | off | +~1 candidate/44k words; most picky rules are the filtered style class. |
| `sweeps` | 13 rules | Deterministic $0 rules. OMITTING THE SECTION KILLS ALL OF THEM. Add bespoke `.py`/regex keys here. |
| `smoothing.max_per_1000_words` | preset (5.0 business) | Copy-edit volume ceiling. Drop toward 2.5 for voice-heavy authors (fragments/profanity/initial conjunctions are the product). |
| `residuals.max_per_rule` | 150 | Cap on same-rule findings before overflow is DROPPED. Raise (e.g. 300) when a legit high-count class (numerals) would overflow. |
| `edit_guard.max_added_chars` | 16 | Max chars an edit may ADD before it must ride as a query. Raise ONLY for hand-verified composites. |
| `genre_scans.reading_level` | on w/ pack | Turn off when it's noise for the genre. |
| `low_confidence` (`confirm`) | on | Recovers stranded low-conf edits (compound_sentence_comma / comma_splice in dialogue). |
| `repair` | trigger ≥3 flags/sentence | Broken sentences fixed as atomic clusters via Fable. |
| `meaning_check` / `fix_check` | on | The two gates. `meaning` punctuation/case-only diffs bypass to `fix` only (deterministic) — do not disable to "save a hold". |
| `chapter_sweep` | off | ~$18 Fable / ~$3 Sonnet, or $0 as subagents. Plan line, never default. |
| `sapling` | off | ~$34/long novel, key is FLY-ONLY (no-ops silently local). Explicit char budget or leave off. |
| `tracked_changes_policy` | `abort` | `accept_all_first` to review a doc that already has tracked changes. |
| `recurrence` | on | Propagates a fix to identical surfaces — degenerate-surface flood guard is in ≥v0.116. |

## Channels (the demotion trap)

`general_error` is **query-channel** — replayed EDIT rows on it silently become
comments. Curated/replayed edits must ride a custom EDIT-channel type
(`curated_fix`-style) declared in `error_types`, listed FIRST so composites win
their spans.

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
