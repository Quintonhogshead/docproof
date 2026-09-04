# KNOBS — the rack's control surface, distilled

This file exists so you **never `cat` `config.py`, `default.yaml`, `--help`, or
`sweeps.py` into your context.** Those reads are 60–90k characters that then
ride your context every turn for the rest of the run — the single biggest
cache-read cost we've measured. Everything you need to write a run config is
here. If a knob you need genuinely isn't documented here, that is an
ESCALATION (the knob may not exist), not a reason to go read the source.

## What changed (the unattended driver + mechanical-only scope)

- **`docproof galley drive --book B --slug S`** — runs the whole loop with
  nobody watching: seeds the workspace, then `profile → approve → sweeps →
  ladder → audit → verify → settle → certify → deliver`, each as its own lean
  headless session with the phase prompts that now live in `galley/driver.py`
  (`~/galley-bin/galley-run.sh` is a thin wrapper over this verb;
  `--print-prompt PHASE` prints one). `--approve auto|email|manual` decides the
  plan gate; `--budget USD` (default **$10**, the API ceiling for a book) is
  the figure it decides against AND the figure the `approve` phase freezes into
  `approval.json`;
  `--from PHASE` / `--phases …` restart or narrow; `--handoff DIR` and
  `--drive-folder-id` put the five hand-off files (`<surname> - Book 2.docx`,
  `… - letter.md`, `… - style-sheet.md`, `… - decision-log.md`,
  `… - outcome.json`) where DocWatch reads them. The book it reads is
  `<surname> - Book 1.docx`, the developmental edit.
  Exit 0 = the run finished; 7 = it stopped and `runs/outcome.json` says
  `needs_human` and why; 2 = a setup error.
- **Mechanical-only scope (go-live).** `flights` and `reread` are not run.
  `docproof galley approve … --stage mechanical-wave --mechanical-only`
  stamps `mechanical_only: true` on `approval.json` and REFUSES over a config
  with `smoothing`/`smoothing.edits`/`rewrite` on; `docproof galley certify`
  then adds a **mechanical only** check that FAILS on a copy-edit lane in the
  config or approval, a shipped copy-edit finding, or a `flights_findings.json`
  in the run dir.
- **Session caps.** Each phase runs `claude --max-turns N` under a wall-clock
  timeout: 400 turns / 4h for settle, 250 / 4h for verify, 100 / 3h for the
  ladder, 60-150 / 2h elsewhere. `--max-turns N` / `--timeout HOURS` override
  every phase, `--phase-max-turns PHASE=N` / `--phase-timeout PHASE=HOURS` one.
  Hitting either ends the run as needs_human naming the phase and the cap.
- **The settle policy.** The driver's settle phase runs `--until-clean --rounds
  3 --quiet-floor 4 --quiet-share 0`: at most 3 rounds, a round raising FEWER
  THAN 5 new items is quiet (the floor is inclusive: `new_items <= floor`), and
  the percentage rule is off so the absolute count decides alone. A sweep still
  noisy at the ceiling ends the run as needs_human ("still finding errors after
  3 rounds: N in the last round"). `--settle-rounds` / `--settle-quiet-floor` /
  `--settle-quiet-share` change the three numbers. NOTE: `--until-clean` now
  treats an EXPLICIT `--rounds N` as its ceiling (it used to ignore it and
  sweep to 12); leave `--rounds` off to keep the old reach.
- **The decision log.** `docproof galley journal RUN --workspace WS --out FILE`
  renders `DECISION_LOG.md` — every action and the reason recorded for it, from
  the run's own artifacts. $0, no model, no clock. The driver writes it into
  `deliverable/` and the hand-off; regenerate it whenever someone asks how a
  decision was made.
- **An escalation stops an unattended run.** Anything a phase appends to
  `QUESTIONS.md` stops the driver as `needs_human` with your question as the
  reason (`--no-question-gate` disables it) — `galley ask` exits 0, so the file
  is the only honest signal that a session asked something.
- **The state machine is the driver's gate too.** After each phase the driver
  requires `state.json` to have reached that phase's state (`intake`,
  `plan_approved`, `mechanical_complete`, `audited`, `settled`, `certified`,
  `delivered`) and stops the run if it has not. Advance it with BOTH `--source`
  and `--config`, every time.

## What changed in v0.183.0 (residual settlement)

- **`docproof galley settle RUN --source BOOK --config C`** — the settlement
  loop. Reads `finished_walk.json` + `change_verify.json`, closes every item
  (absorb / add / drop / query), rebuilds at $0, re-verifies the touched
  paragraphs, repeats to `--rounds` (default 3); leftovers ship as
  `unresolved_after_N` queries. Writes `settlement.json`, restamps the two
  verify artifacts for the final build, stamps `state`/`disposition_reason`
  on every findings row, writes `outcome.json`. `--engine auto|subagent|
  provider|none`; `--no-verify` skips the delta re-read; `--dry-run` lists
  the open items with owner resolution.
- **`--until-clean`** on settle: keep sweeping while rounds keep finding real
  work; stop after a QUIET round (new items ≤ `--quiet-floor` 3, or ≤
  `--quiet-share` 0.02 of the edits+paragraphs the round re-read), or when
  `--max-turns` (400) is spent, never past 12 rounds. A sweep that has to
  stop while still noisy after 2+ rounds makes `outcome.json` say
  **needs_human** ("still finding N new errors per round"). Settlement rows
  on untouched text PROPAGATE to the identical word-shaped surface in the
  same and neighbouring paragraphs (`--no-propagate` to disable); a quote
  repeated in one paragraph settles all its sites.
- **`docproof galley residuals RUN [--source BOOK --config C] [--json]`** —
  the open-items list (residual / edit_damage, owner, resolution).
- **`docproof galley outcome RUN [--set done|needs_human --reason …]`** — the
  verdict + HubSpot fields to `outcome.json` (`--done-value`,
  `--needs-human-value`, `--rewrite-share`, `--edit-density`).
- **`galley verify --engine subagent`** runs both gates on the $0
  subscription lane; **`--paragraphs a,b,c` / `@FILE`** verifies a delta.
- **`galley state --advance settled --results RUN`** — new state between
  `audited` and `certified`; refuses (exit 7) while anything is open.
- **`import-findings --anchor accepted --run RUN`** — rows quoted from the
  ACCEPTED text fold into that build through its edit map.
- Every build now writes **`editmap.json`** (source→accepted segments per
  paragraph with the owning row ids). Replay/import now carry `lane`,
  `cluster_id`, `silent`, `withheld` through a rebuild, and strip editorial
  notes from imported corrections at intake (a note-only row is rejected).
- certify gained **terminal states**, **residual settlement**, and
  **outcome** checks; the walk check fails on ANY unsettled residual (not
  just high), the change-verifier check on any unsettled flagged edit.

## What changed in v0.182.0 (Redding run fixes)

- **`docproof merge` enforces strict cross-lane non-overlap** — a copy-edit row overlapping any
  mechanical span loses (ledgered "mechanics win overlaps (strict)"); subsumption only when the
  rewrite contains the mechanical correction verbatim AND passes sweeps+LT. The artifact loop
  drops the whole parent of a split member and converges.
- **`galley flights` pre-filter** drops malformed proposals (`malformed_head`, `malformed_shape`),
  strips or drops editorial notes inside a replacement (`editorial_note`), and the judge demotes
  any accepted change that alters a number, a name, or a title to a QUERY.
- **`number_style` never touches labels** ("Mindset Number 23", "Chapter 4", "3)").
- **`sweep_terminal_period` skips attribution, display, and template lines; the dialogue-tag
  rule never periods a tag that is followed by an opening quote.**
- **`galley certify` marks `change_verify.json`/`finished_walk.json` older than the build as
  stale (skip), never reports a previous build's verdict.**
- **`ensemble.enabled: false` now disables the ensemble** even with detectors listed.
- **New stage `poetry-touch` + genre `poetry`**: verse is proofread for real-word misspellings only; everything else locked off.
- **`review --dry-run` / `flights --dry-run` no longer build the storysheet provider** (they
  would have called Luna on a dry run).

## What changed (galley CLI contract fixes)

- **`flights.posture` defaults to `lenient`** (config + CLI) — the copy-edit
  lane offers rejectable tracked changes, the author decides; genre packs
  still set it (academic / literary_memoir / general_* → strict). The flights
  **judge defaults to `gpt-5.6-luna`** (or `flights.judge_model` when a config
  names one) — a Claude model passed to `--judge-model` BILLS via the API; the
  $0 Claude route is `export-judgments` → session subagent → `import-judgments`.
  The $0 subagent-proposer route is `flights IN --models "" --external-proposals
  P.json` (never `--judge-only`, which reads a clusters.json).
- **`galley audit` / `galley verify` / `galley flights` take `--approval
  approval.json` and `--budget USD`** — refuse (exit 5) when a model the verb
  would call is outside the approval's allowed models/providers or the planned
  spend exceeds the cap; `galley verify --dry-run` prints the priced call count
  (one per 30 applied edits + one per ~6k chars of accepted text). `review
  --approval` now hashes the config AFTER `--rounds`/`--meaning-*`/`--fix-*`
  land, so a flag added past approval is a refused deviation.
- **Every paid galley artifact carries `cost: {total_usd, by_model}`**
  (audit.json, change_verify.json, finished_walk.json — each verify gate bills
  its own file — and flights_findings.json); `review` prints its dollar line;
  `galley profile --model` reports the call's spend on stderr.
- **`galley verify` exits nonzero only for a flagged applied edit or a
  HIGH-severity residual**; low/medium residuals print as notes (certify draws
  the same line).
- **`galley score` grades a `docproof review` findings.json directly** (rows
  convert through the docproof-ladder adapter: anchored, validated rows only);
  **`galley calibrate --from-run RUN`** synthesizes the case file from a bare
  run's findings.json when no casefile.json exists.
- `docproof sweep` / `docproof merge` are $0 for real: the storysheet read and
  the candidate-screening judge (both prepare()-time spenders a mechanical-wave
  config ships on) are zeroed with every other paid pass.
- **`galley state`**: pass BOTH `--source` and `--config` on every `--advance`
  and on `--verify-resume` — the resume check is strict about a missing hash.

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
  `historical`, `religious`, `self_help_business`, `poetry`. Run theological non-fiction
  under `religious`, never `self_help_business` (that one turns edits + rewrite
  on). Run verse ONLY as `--genre poetry --stage poetry-touch`: the stage keeps
  `error_types: [spelling]` + the spell scan and locks every other lane, sweep,
  and gate off — feather-soft, spelling only.
- **`docproof galley approve`** freezes the composed config into `approval.json`
  (source + config hashes, allowed models/providers, stage, lanes, budget, and
  `mechanical_only` when `--mechanical-only` is given).
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
| `recurrence` | on | Propagates a fix to identical surfaces — degenerate-surface flood guard is in ≥v0.116; never seeds from a curated/imported row. |
| `variant` | `auto` under mechanical-wave (base `us`) | `auto` DETECTS the book's English from its own spelling (British vs American markers) and proofs against THAT — a British book read as British, not Americanized; the spell scan + respell follow the detected dictionary. Resolves to `us` on thin/mixed evidence; ca/au are hybrids you still set explicitly (they carry `confirm`). Logged, never silent. |
| `storysheet.enabled` / `.model` | ON + `gpt-5.6-luna` under mechanical-wave (base off) | One Luna read that pins narrator/person/tense/character-pronouns into every detector's prompt. Cheap, and it lifts every downstream pass; pairs with `variant: auto` as the voice+spelling profile. |
| `continuity` (whole-book) | **default ON as a $0 Opus subagent** in the loop (base off; config model is Luna) | Timeline/age/date arithmetic, attribute + object drift across the whole book, plus the deterministic $0 calendar (weekday-vs-date) check. Query-only. Run it as an Opus session subagent, import the queries — do NOT enable it in the paid review config (that bills). |
| `chapter_continuity` | **default ON as a $0 Opus subagent** in the loop (base off) | Intra-chapter physical continuity (sat who never stood, cigarette lit twice, dawn→evening). Query-only. Same $0 path: an Opus subagent per chapter, imported. |
| `rewrite` ("type-and-compare") | **default ON as a $0 Sonnet→Opus subagent** in the loop (base off; the stage LOCKS the in-config lane off) | Retype each paragraph as a minimal proofread, diff against the source — catches the missing-word/homophone/agreement misses detection glides past (~25% of core-mechanical misses). Run as a subagent detector, import findings; the P0-1 guard keeps a real-word swap it proposes a query, never a blind edit. |
| `flights.posture` | `lenient` | The copy-edit judge's stance: lenient offers every defensible change as a rejectable tracked edit (same hard vetoes either way); `strict` keeps the original by default. Genre packs set it; `--posture` overrides per run. |
| `candidate_screening.mode` | `apply` under mechanical-wave (base `off`); judge = `gpt-5.6-luna` | The low-precision generators propose, the Luna judge rules, affirmed rows apply as tracked edits. Apply is contained: it stays SHADOW unless the deployment sets `DOCPROOF_CANDIDATE_APPLY=1` — the Galley launch does, production keeps the floor. |

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

### Settlement artifacts (v0.183.0)

- **`editmap.json`** (every build): `{"paragraphs": {para_id: [{"src":
  [s0,s1], "acc": [a0,a1], "text": …, "owner": key_id|null, "rows": [ids]}]},
  "owner_of": {finding_id: key_id}, "unmapped": {para_id: why}}`. The
  accepted paragraph is the concatenation of `text`; `owner` null = untouched.
- **`settlement.json`**: `{"rounds", "engine", "model", "counts": {action:
  n}, "records": [{"residual_id", "round", "action": absorb|add|revise|drop|
  query, "owner_finding_id", "before_replacement", "after_replacement",
  "reason", "verified_by", "para_id", "question", "kind": residual|
  edit_damage}], "open": [], "residuals_seen": [...], "cost", "notes"}`.
  Reason prefixes: `duplicate | overlap_loser | voice | intent_zone |
  style_only | fact | unanchorable | walker_wrong | unresolved_after_N |
  ambiguous_anchor | editorial_note | verifier_reverted | oversize |
  space_deletion | rejected_* | no_suggestion | edit_damage:<verdict>`.
- **`outcome.json`**: `{"outcome": done|needs_human, "reason", "evidence":
  {words, applied_edits, edit_density_per_kword, rewrite_share,
  unresolved_queries, edit_damage, …}, "hubspot": {"object": "0-970",
  "property": "docproof", "value": …}, "set_by": assess|human}`.
- Settlement rows in `findings.json` ride `error_type: galley_settle` with
  `chunk_id: settle:<residual_id>`; every row carries `state`
  (applied|dropped|query) and `disposition_reason` after settle.

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
