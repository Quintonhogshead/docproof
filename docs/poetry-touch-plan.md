# Poetry touch v2: verse mechanics, not spelling only

Status: steps 1–5 implemented 2026-09-15 (v0.212.0, `fixed-proofreading-v6`);
step 6 (tests) shipped with them; step 7 (Dalton re-run) pending. Source of truth for what a poetry proofread
should do is the head proofreader's change list on Chantal Dalton's
*Monologues for Two* ("Chantal Dalton changes.docx", Chris Beale).

## What the proofreader actually does to verse

The list is a full Atmosphere mechanical proofread applied at the
character and word level, with the poem's *structure* left alone.

Touched (tracked unless noted):

| Category | Count | Example |
|---|---|---|
| Double spaces (untracked) | 32 | collapse |
| Three periods to house ellipsis | 1 | NBSP before, space after |
| Elision apostrophe direction | 1 | `‘bout` to `’bout` |
| Hyphens / en dashes / `--` / `---` used as sentence dashes, spaces around dashes | 25 | `breasts ---` to `breasts—`; `– just old` to `—just old`; `-–` to `—`; `–or` to `—or`; one comma dropped before a dash |
| Clock time | 1 | `3pm` to a digits-plus-meridiem form (house: `3:00 PM`) |
| Number range | 1 | `Aug 13-26` to `Aug 13–26` |
| Numbers one to one hundred spelled out | 2 | |
| Closed / hyphenated compounds | 3 | `old fashioned`, `corn bread`, `always-open-door` |
| Spelling | 1 | `smokey` to `smoky` |
| Missing / extra words | 3 | `we to do` to `we do`; `reply a comment` to `reply with a comment`; `as impulse` to `as an impulse` (only if it does not break the flow) |
| Proper-name article | 1 | `The Royal Palms` to `the Royal Palms` |
| Comma before an opening parenthesis | 1 | removed |
| Role capitalization | 1 | `an Ambassador` to `an ambassador` |
| Mismatched quote pairs (author's single-quote convention kept) | 2 | `‘We”` to `‘We’` |
| Serial comma | 2 | `kale, and corn`; `aunt, or friend` |
| Query | 1 | `sub-urban` — intentional? |

Not touched: line breaks, line-head capitals, fragments, missing
terminal punctuation, repetition, the single-quote convention, dialect.

## What Galley did on the same book (fixed lane, 2026-09-14)

- Classification: `uncertain`, then per-paragraph fallback marked 1,161 of
  1,180 paragraphs as poetry. Delivered 2 corrections, both in the prose
  covering letter.
- Silent intake normalization (runs before classification, on every
  paragraph) already collapsed the double spaces and set the ellipsis. So
  two of the proofreader's categories are handled, silently.
- Everything else on the list is blocked by design: `configuration(poetry=True)`
  sets `error_types: [spelling]`, `sweeps: []`, excludes poetry ids from
  the local house sweeps and the numbers stage, and the whole-book readers
  are told "Poetry receives spelling only".
- Bug: the adjudication gate in `galley/fixed_workflow.py` (`_adjudicate`)
  accepts a poetry edit only when the single proposer is Sonnet. The Sonnet
  spelling reader found nothing; Sol, Fable and Astra proposed 11 spelling
  fixes (`smokey`, `shutters`/`shudders`, `school yard`, `sun flowers`,
  `pick up`, `catch as catch can`) and every one was dropped. Even the
  spelling-only promise under-delivered.

## Coverage of the existing deterministic sweeps against the list

- `sweep_dash` already handles `--`, `---`, spaced en dash, spaced hyphen,
  and the trailing-line `---`. It misses mixed runs (`-–`, `–--`) and an
  en dash spaced on one side only (`’ –or`): 3 of the 25 dash sites.
- `sweep_ellipsis`, `sweep_trailing_space`: covered (and already applied at intake).
- `sweep_time_of_day`: `3pm` matches and becomes `3:00 PM`. House style
  (`11:00 AM`) wins over the proofreader's `3:00 p.m.`.
- Hyphen between digits (`13-26`): the dash sweep deliberately skips it.
  Needs the numbers stage (which excludes poetry today) or a month-name range rule.
- Wrong-direction curly elision (`‘bout`): nothing handles it. Normalization
  only curls straight marks, and the source already had the wrong curl.
- Serial comma, capitalization of roles, closed compounds, missing words,
  quote pairing, spelled-out numbers: typed detectors (`serial_comma`,
  `capitalization`, `spelling`, `missing_word`, `quote_balance`,
  `number_style`) exist and are simply switched off for verse.

## Doctrine change

Verse keeps its **structure**: lineation, line breaks, line-head capitals,
fragments, absent terminal punctuation, repetition, coinages, dialect, the
author's quote-mark convention. Verse gets the **house mechanics** at the
character and word level exactly as prose does: glyphs and spacing (ellipsis,
dashes, apostrophe direction, double spaces), spelling and closed compounds,
serial comma, role capitalization, clock times, number ranges, spelled-out
numbers, mismatched quote pairs, clear missing or extra words. A word
insertion (an article) is made only when it does not disturb the line; when in
doubt leave it. Terminal periods, dialogue-tag rules, doubled-word and
stacked-punctuation sweeps stay off.

## Implementation plan

1. **Stage and genre config.** `config/stages/poetry-touch.yaml`: error_types
   become `[spelling, homophone_confusion, apostrophe_error, capitalization]`,
   `[serial_comma]`, `[number_style]`, `[missing_word]`, `[ly_adverb_hyphen]`,
   `quote_balance`; sweeps become `sweep_ellipsis, sweep_dash,
   sweep_trailing_space, sweep_time_of_day, sweep_compound_number,
   sweep_century, sweep_decade_apostrophe, sweep_initialism`. Keep
   `sweep_terminal_period`, `sweep_doubled_word`, `sweep_stacked_punctuation`,
   the dialogue sweeps and `sweep_nested_quote` off. Locks unchanged
   (smoothing, rewrite, repair, sapling, chapter sweep). Rewrite the yaml
   descriptions and `config/genres/poetry.yaml`.
2. **Sweeps.** Extend `_sweep_dash` for mixed runs (`-–`, `–-`, `–--`) and a
   one-side-spaced en dash between words. Add `sweep_elision_apostrophe`:
   an opening single curly quote directly before a word in the `_ELISIONS`
   list (shared with `docproof/normalize.py`) becomes a right single quote.
   Add a date-range case: `Month DD-DD` to an en dash.
3. **Fixed lane policy.** `galley/fixed_policy.py::configuration(poetry=True)`
   mirrors the stage instead of hard-coding `["spelling"]` and `sweeps=[]`.
   Add `VERSE_CATEGORIES` (spelling, homophone, apostrophe, capitalization,
   serial_comma, number_style, missing_word, hyphenation, quote_balance,
   punctuation for dash/ellipsis/space glyph swaps) and `VERSE_POLICY`, the
   doctrine paragraph above, given to every reader that sees verse.
4. **Fixed lane workflow.** `galley/fixed_workflow.py`:
   - Typed pass for poetry runs Sonnet and Luna, not Sonnet alone.
   - Local checks: run `_house_findings` with the verse sweep list over
     poetry paragraphs instead of excluding them (`fixed_local._paragraphs`
     gets a verse mode; `excluded_poetry_ids` becomes `verse_ids` with the
     sweep list recorded in the evidence packet).
   - Numbers stage includes poetry paragraphs with the verse rider.
   - `_adjudicate`: replace the "single Sonnet spelling proposer" gate with
     the category allowlist, any model, confidence not low; disputed groups
     go to the normal screen.
   - `_apply`: replace `spelling_only` with a verse guard that forbids
     newline changes, case changes on a line-head word, and inserting a
     terminal mark, and allows everything in the allowlist.
   - Whole-book readers (ensemble sweep, Fable, Astra) and continuity get
     `VERSE_POLICY` instead of "receives no edits".
   - Bump `VERSION` to `fixed-proofreading-v6`; register it in
     `galley/fixed_documents.py` with the poetry stage list
     `["poetry", "typed", "numbers", "checks", "poetry_complete"]`.
5. **Doctrine text.** Replace "Poetry receives spelling only" in
   `galley/press_prompt.py` (three sites), `galley/practitioner/CLAUDE.md`,
   `references/config.md`, `references/intake.md`, `docs/fixed-galley.md`,
   `docs/galley-press-editorial-brief.md`.
6. **Tests.** Rewrite `tests/test_poetry_touch.py`,
   `test_galley_fixed_policy.py::test_poetry_is_spelling_only…`, and the
   poetry cases in `test_galley_fixed_workflow.py` (`…only_classification_and_sonnet_spelling`,
   `…never_invokes_local_collectors`, `…cannot_emit_a_candidate_in_embedded_poetry`)
   to assert the verse allowlist and the structural guard. Add sweep unit
   tests for every dash form in the Dalton list and for `‘bout`.
7. **Validation.** Re-run Dalton 2 locally in a fresh workspace (recipe in
   memory `galley-fixed-local-run-recipe`) and diff the tracked changes
   against the proofreader's list. Target: every deterministic item (dashes,
   ellipsis, time, range, elision) and at least the spelling, compound,
   serial-comma and quote-pair items; zero edits to line breaks or
   line-head capitals. Then bump `docproof/__init__.py`.
