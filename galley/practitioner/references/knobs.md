# Knob lookup — read only matching rows

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
| `ensemble` | off | Luna+Sonnet union + a verifier — the recall wave-1 recipe. Ready-made as `--stage mechanical-wave` (see `references/config.md`); fires ONLY through `error_types`. |
| `error_types` | full shipped list | The typed LLM passes. **OMITTING THE SECTION ZEROES EVERY PASS** and makes any `ensemble:` inert (`0 error type(s) in 0 pass(es)`). Restate the full default list to keep them; the ensemble only fires through these. |
| `error_types[key]` | `{group,passes,token_budget}` | Per-category repeat reads. `passes:2` = union re-read (house-comma recipe); costs ~2× that category. Custom EDIT-channel replay types go here (see `references/findings.md`). |
| `languagetool.picky` | off | +~1 candidate/44k words; most picky rules are the filtered style class. |
| `languagetool.edit_word_replacements` | off | Whether an LT suggestion that swaps one WORD for another (boop→book, sesh→mesh) may become a tracked edit. OFF: every real-word swap routes to a margin QUERY instead — LT still edits punctuation/spacing/casing/en-dash blind, still ASKS about a word change, but never applies one silently (Purpura: 35 auto-applied corruptions). LT also never de-accents a word MW spells with the accent (cliché), and now respects the protected-noun allowlist for EVERY rule, not just misspellings. Turn on only for a house that has measured LT word edits safe. |
| `sweeps` | 16 rules | Deterministic $0 rules. OMITTING THE SECTION KILLS ALL OF THEM. Built-in keys ONLY — Config rejects a path or a bespoke key ("unknown sweep"). Bespoke rules run through `docproof sweep IN --rule F` and fold in as `import-findings` rows (see `references/sweeps.md`). |
| `smoothing.max_per_1000_words` | preset (5.0 business) | Copy-edit volume ceiling. Drop toward 2.5 for voice-heavy authors (fragments/profanity/initial conjunctions are the product). |
| `residuals.max_per_rule` | 150 | Cap on same-rule findings before overflow is DROPPED. Raise (e.g. 300) when a legit high-count class (numerals) would overflow. |
| `edit_guard.max_added_chars` | 16 | Max chars an edit may ADD before it must ride as a query. Raise ONLY for hand-verified composites. |
| `genre_scans.reading_level` | on w/ pack | Turn off when it's noise for the genre. |
| `low_confidence` (`confirm`) | on | Recovers stranded low-conf edits (compound_sentence_comma / comma_splice in dialogue). |
| `repair` | trigger ≥3 flags/sentence | Broken sentences fixed as atomic clusters via Fable. |
| `meaning_check` / `fix_check` | off in base; ON via `--stage mechanical-wave` | The two gates. The BASE config ships them off; the mechanical-wave stage turns both on (that stage IS the "both gates" recipe). `meaning` punctuation/case-only diffs bypass to `fix` only (deterministic) — do not disable to "save a hold". |
| `chapter_sweep` | **ON under `--stage mechanical-wave`, model `gpt-5.6-luna`** (base off) | The six-window chapter sweep is Galley's FIRST lane on every book — wave 1 line 1, before the ladder (Quinton, 2026-09-04: best bang for the buck, by far). Luna ≈ $1–2/book paid; PLUS a six-window Sonnet sweep as $0 session subagents, imported. Fable ≈ $18 only when a thread spans the book. The driver's mechanical-wave stage enables it; a bare `review` config still ships it off. |
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
