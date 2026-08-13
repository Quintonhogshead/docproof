# Integrating the Atmosphere three-pass proofreading prompt

The proofreading team runs a long chat prompt — *Atmosphere Press —
Proofreading Prompt (Three-Pass)* — against manuscripts by hand. This is the
plan for absorbing it into docproof.

## What the prompt actually is

Three different things stapled together, and each has a different home here:

1. **Judgment rules** (serial comma, that/which, dialogue mechanics, voice
   preservation) → error types. `config/error_types/*.yaml`, no code.
2. **Pattern rules** (ellipses, dashes, stacked punctuation, centuries, the
   dialogue-tag table) → deterministic sweeps. These must never reach a model:
   the prompt itself demands exact zero-remaining match counts, and only a
   script can honestly report one.
3. **Identity and policy** (author name, English variant, filenames,
   deliverables) → config and reporting.

The prompt says this itself: *"Pattern rules are executed, not read for…
Reading catches judgment; scripts catch patterns — do not use one for the
other's job."* docproof is the "script" half made real, so roughly half the
document should never go near a prompt.

## What deliberately does not transfer

The three-pass requirement, the time expectations, and most of the honesty
constraints are compensations for a chat agent whose attention fades over a
long context. docproof's architecture — small chunks, one error class per
pass, deterministic anchoring, confidence gating — is the *alternative*
solution to that same problem. Running the pipeline three times would triple
cost for little gain. The honest equivalent is the reject-all audit plus the
scripted-check report, which give the team a stronger guarantee than "I
promise I read it three times."

---

## Phase 1 — House rules as error types — **done**

Config and YAML only; no code changes. Shippable on its own.

1. `revision_author: Atmosphere Press Proofreader` in `config/default.yaml`.
2. New error types, do-not-flag lists written first, each with calibration
   examples including negatives:
   - `serial_comma`, `complex_list_semicolon`
   - `introductory_comma`, `direct_address_comma`, `tag_question_comma`
   - `dialogue_tag`
   - `number_style`, `currency_style`, `ly_adverb_hyphen`
   - `that_which`, `preposition_error`
3. Cross-references added to the shipped types that could claim the same span
   (`capitalization` ↔ `dialogue_tag`, `apostrophe_error` ↔ `number_style`),
   the way `comma_splice` and `run_on_sentence` already point at each other.
4. Regroup `error_types` passes, keeping precision order: objective types
   run first so they win overlap collisions.

**Deferred out of this phase:** `speaker_change` (one speaker per paragraph).
The prompt says to flag it and explicitly not to restructure — so the finding
has no `corrected_text`, and the validator rejects a no-op edit
(`rejected_noop`, `validator.py`). A flag-only type needs the comment-only
channel from Phase 4. It lands there instead.

**Done when:** a review applies Atmosphere house rules under the right author
name, with acceptable per-type false-positive rates.

## Phase 2 — Deterministic sweeps and the spell scan — **done**

The largest code change, and the foundation the rest leans on. Shipped as
`docproof/sweeps.py` and `docproof/spellscan.py`; see `docs/sweeps.md` for how
they behave and how to configure them.

Two items named below moved deliberately. The **numbers candidate sweep** is
not a sweep: bare integers need the legitimate-numeral judgment that Phase 1's
`number_style` error type already makes, and a pattern that auto-corrected
them would edit dates and addresses. It belongs with the spell scan's
candidate mechanism — feeding the model rather than editing — and is the
natural next increment. The **dialogue-tag sweep covers pronoun subjects
only**, which is exactly the brief's table; a named subject stays with the
error type, because lowercasing a character's name is worse than missing a
comma.

### Sweep engine

New `docproof/sweeps.py`. Each sweep is a pure function over paragraph text
emitting synthetic `Finding` objects plus a `SweepReport` of counts. Because
sweeps run over the `DocumentModel`, footnotes and endnotes are covered free —
`walk_package` already ingests them, which satisfies the prompt's Operating
Principle 8.

Sweeps: stacked punctuation · three-dot ellipsis → `…`, spaced to the house
convention (`style.ellipsis`, default `nbsp` — a non-breaking space before) ·
hyphen-run dashes (`--`/`---`, en dash between numbers, em dash in prose) and
closing up a spaced em dash ·
doubled words · centuries · numbers candidate detection feeding the Phase 1
`number_style` judgment · the dialogue-tag exhaustive table, all cells
enumerated as data, both character orders.

Sweeps run *before* the model passes, so their findings get first claim on a
span in the validator. A post-fix re-scan asserts zero remaining matches per
sweep — the honest counts the prompt demands. Once the sweep owns the
enumerable dialogue-tag cells, the `dialogue_tag` error type narrows to the
judgment cases, and the pass count comes back down.

### Spell scan

A deterministic scan that **never auto-fixes**. Out-of-dictionary is evidence,
not an error — the prompt's own horror story is spell-checking a coined word
into standard English.

1. Tokenize the whole `DocumentModel` into a case-aware frequency lexicon,
   keeping hyphenated compounds intact.
2. Check against a bundled offline Hunspell dictionary (`spylls` — pure
   Python, no native build) plus a house allowlist. Offline matters: tests
   must not reach the network, and the watcher runs unattended. Hunspell also
   ships `en_US`/`en_GB`/`en_CA`/`en_AU`, which plugs into Phase 5's variant
   switch.
3. Classify, don't correct:
   - repeated or consistently capitalized unknowns → presumed coined terms and
     proper nouns, collected into a run-scoped author lexicon;
   - singleton unknowns with a close dictionary neighbour → spelling
     *candidates*.
4. Route the evidence: the author lexicon is injected into the `spelling` /
   `homophone_confusion` pass as a document-specific do-not-flag list, and the
   candidates go in as targeted hints. This is the part that pays for itself —
   it attacks the exact false-positive mode the error-type system is built
   around, with data no static list can have.
5. Report tokens scanned, unknowns, lexicon size, candidates raised and
   confirmed, in the same ledger as the sweeps.

Byproduct: the frequency lexicon is the same structure Phase 5's consistency
engine needs, so that phase becomes an analysis over data already collected.

New `sweeps:` and `spellcheck:` config sections, added to `config.py`,
`default.yaml` and their consumers together. Bundled dictionaries need a line
in `DocProof.spec` datas so PyInstaller builds keep working.

**Done when:** a mock-provider run produces correct tracked changes for every
sweep category with exact match counts, at no API cost.

## Phase 3 — Silent normalizations and the reject-all audit — **done**

These ship together: the audit's definition depends on the normalizations.
Shipped as `docproof/normalize.py` and `docproof/audit.py`; see
`docs/audit.md`.

Two things came out differently from the sketch below, both for the better.
The audit does **not** need to know about the two exceptions: because
normalization runs before ingest, the invariant is simply "rejecting every
tracked change reproduces the document as ingested", with no exceptions at
all. And quote curling **does not convert everything** — a single mark whose
direction the text does not settle is left straight and counted, because a
mark curled the wrong way ships silently while a straight one is merely a
blemish a proofreader will catch.

1. Package-level normalization before ingest — straight→curly quotes and
   apostrophes, multi-space collapse (preserving leading indentation and
   deliberate dividers like `*   *   *`). Applied to the XML directly,
   untracked, with counts recorded. `content_hash` is computed after
   normalization so checkpoints and batch collection stay coherent.
2. Audit: after save, programmatically reject every revision in the output and
   diff its text against the pre-normalization original. The only permitted
   differences are the two normalization categories. A mismatch fails the run —
   the same refuse-to-ship posture as `prep/verify.py`.

**Done when:** the audit passes on real runs, and a deliberately injected
untracked edit makes it fail.

## Phase 4 — Two channels and the team's deliverables — **done**

Shipped as the query channel (`channel: query` on an error type, plus
below-gate findings), `docproof/changelog.py`, and the house filenames on
`DocumentFormat`. See `docs/deliverables.md`.

Building it surfaced a bug in Phases 2–3 that had nothing to do with Phase 4.
`min_paragraph_chars` was dropping short paragraphs at *ingest*, so the sweeps
never saw them — and in fiction a short paragraph is a line of dialogue, which
is exactly where a stray `?!` or a mispunctuated tag lives. Every sweep's "zero
remaining" therefore meant "zero remaining in the long paragraphs". Paragraphs
now carry a `reviewable` flag: the sweeps read all of them and only the model
passes skip the short ones, so the cost is unchanged and the count is true.


1. **Comment-only queries.** Findings below `min_confidence`, and flag-only
   types like `speaker_change`, become Word comments anchored to their text
   with no `w:ins`/`w:del`. This completes the prompt's two-channel model:
   tracked changes are corrections, comments are questions. Today the below-gate
   findings only reach `summary.md`.
2. **The change log `.docx`**, generated from `findings.json`: style basis and
   variant assumption, corrections table with reasons, the scripted-check
   report from Phase 2, query list, "deliberately left unchanged" (rejected and
   below-gate findings with reasons), limits-of-this-pass note, footnote
   coverage statement, and the Phase 3 audit confirmation.
3. **Filenames** in `fmt.reviewed_name`: ` - Atmosphere Press Proofreader` and
   ` - Atmosphere Press Proofreader Change Log`.

**Done when:** a run's two output files match what the team currently produces
by hand, and queries appear as margin comments in Word.

## Phase 5 — Variants and the two hard problems — **done**

All three shipped. Variants are `docproof/variants.py` and
`config/variants/*.yaml`, wired into the model prompts, the dialogue-tag sweep,
the normalizer, the spell scan and the change log (`docs/variants.md`). Title
italics and term consistency are `config/error_types/title_italics.yaml` with
`apply_format_change` in the reassembler, and `docproof/consistency.py`
(`docs/whole-document.md`).

Both hard problems came out as new *channels* rather than new machinery. An
error type declares whether its findings are changes, questions or formatting,
and the channel decides what `corrected_text` means — the fixed sentence, the
sentence repeated unchanged, or the span to mark. Term consistency needed no
error type at all: there is no prompt to write, because the whole thing is
decided before any model sees the document.

Two limits worth carrying forward. The reject-all audit compares text, so a
formatting revision passes it trivially — correct, but it means formatting
applied *without* a revision would pass unnoticed; the baseline would need to
carry run properties to close that. And consistency runs on whole-document
reviews only, because "you write this three ways" is not a claim a review of
two chapters can make.

It came out lighter than the sketch below. Variants are **one small file of
conventions each**, injected into every pass the way the manuscript's own
vocabulary is, rather than four shadowed copies of every error type — four
copies drift, and a do-not-flag list fixed in the U.S. file would quietly stay
broken in the other three. `error_type_override_dir` remains the escape hatch
for a variant that needs a genuinely different type rather than a different
convention.

One limitation worth stating plainly: `spylls` bundles `en_US` only, so a
U.K., Canadian or Australian run has no dictionary scan unless the press points
`spellcheck.dictionary` at its own Hunspell files. Checking British spelling
against an American word list would file every correct `colour` as unknown, so
the scan declines, says why, and the change log records it as a limit.


1. **English variants.** A `variant: us | uk | ca | au` config field resolving
   to `config/variants/<v>/`, which shadows base error types through the
   existing `error_type_override_dir` mechanism, plus variant parameters for
   the sweeps (decade apostrophes, percent vs. per cent, date and time formats,
   quote-nesting order) and the spell-scan dictionary. No auto-inference: the
   team states the variant, the change log records it.
2. **Title italics.** A formatting-change finding kind (`format: italic`
   instead of `corrected_text`) with reassembler support for tracked run
   formatting. The current finding model can only express text edits, so this
   needs new shape, not just a new prompt.
3. **Consistency engine.** A document-wide scan over the Phase 2 frequency
   lexicon for compound and coined-term variants (`blood-cursed` /
   `bloodcursed`), then one adjudication pass to pick the dominant form.
   Outliers become tracked changes or queries. This is the piece per-chunk
   review structurally cannot do.

**Done when:** a UK manuscript flips every variant-dependent rule end to end,
and a fantasy manuscript gets coined-term consistency without a single
invented word "corrected" into standard English.

---

Every new config field lands in `config.py`, `config/default.yaml` and its
consumers in the same commit. The desktop app inherits all of this through
`prepare`/`finish` without changes until Phase 4's filenames.
