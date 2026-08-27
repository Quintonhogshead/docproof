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

# The 15 deterministic $0 sweeps. Omitting the section turns ALL of them off.
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
  - sweep_trailing_space
```

To ADD a per-category repeat read or a bespoke sweep, paste the block above and
append/modify — don't hand-pick a subset unless you mean to drop the rest.
**KNOBS.md is the authority: if a knob is listed here it exists — trust it over
a source `grep`.** (`languagetool.picky` is real — config.py:679 — and was once
wrongly dropped on a bad grep.)

## Top-level sections (names are exact)

`api · chunking · skip · normalize · speaker_split · spellcheck · consistency · glossary ·`
`factcheck · genre_scans · flights · continuity · chapter_continuity · rounds ·`
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
| `ensemble` | off | Luna+Haiku union + a verifier — the recall wave-1 recipe. Ready-made as `--stage mechanical-wave` (see the ensemble block above); fires ONLY through `error_types`. |
| `error_types` | full shipped list | The typed LLM passes. **OMITTING THE SECTION ZEROES EVERY PASS** and makes any `ensemble:` inert (`0 error type(s) in 0 pass(es)`). Restate the full default list to keep them; the ensemble only fires through these. |
| `error_types[key]` | `{group,passes,token_budget}` | Per-category repeat reads. `passes:2` = union re-read (house-comma recipe); costs ~2× that category. Custom EDIT-channel replay types go here (see below). |
| `languagetool.picky` | off | +~1 candidate/44k words; most picky rules are the filtered style class. |
| `sweeps` | 15 rules | Deterministic $0 rules. OMITTING THE SECTION KILLS ALL OF THEM. Add bespoke `.py`/regex keys here. |
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
