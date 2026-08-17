# Error types and passes

Everything docproof looks for is defined in `config/error_types/*.yaml`. Adding
a check is authoring a YAML file and enabling its key — the pipeline never needs
code changes for a new type. If it does, that's a bug in the pipeline, not a
limitation of the format.

## Passes: what grouping is for

`error_types` in `config/default.yaml` is a list of **passes**, not a list of
types. Each entry is one API call per chunk:

```yaml
error_types:
  - [repeated_word, spelling, homophone_confusion, apostrophe_error, capitalization]
  - [serial_comma, complex_list_semicolon, introductory_comma,
     direct_address_comma, tag_question_comma]
  - dialogue_tag
  - [number_style, currency_style, ly_adverb_hyphen]
  - [comma_splice, run_on_sentence, subject_verb_agreement, that_which]
  - [tense_shift, pronoun_agreement, missing_word, preposition_error]
```

A bare string runs alone; a list runs as one combined pass.

### Tuning one category: repeats and chunk size

An entry can also be a mapping, to tune a single category without touching the
global budget or the other categories:

```yaml
error_types:
  - group: [serial_comma, introductory_comma, direct_address_comma]
    passes: 2            # read this category twice (default 1)
    token_budget: 1800   # chunk this category smaller (default: chunking.token_budget)
```

- **`passes`** reads the category N times. The validator keeps one edit per span
  (`rejected_duplicate`), so a second read surfaces stragglers the first missed
  without doubling the applied edits — a recall lever for a hard category, at N×
  its cost.
- **`token_budget`** chunks *this* category at its own size. Fewer paragraphs
  per call means tighter attention and better recall on dense categories like
  commas; the trade is more requests. The default-budget categories are
  unaffected, and their chunking — and every cache and batch id built from it —
  is byte-for-byte unchanged.

Both keys are optional, and a bare key or list is exactly a category with
`passes: 1` and the global budget. `docproof inventory <file>` prints the
expanded plan (each read, each budget) and the resulting request count.

This exists because cost is dominated by re-sending the document. One pass per
type means N copies of the manuscript in input tokens; grouping collapses that
to one copy per group. The system prompt grows instead, and it's cached, so
after the first chunk it's nearly free. Going from 22 solo passes to 6 grouped
ones cuts document input tokens by about 73%.

`docproof inventory <file>` prints the arithmetic before you spend anything:

```bash
.venv/bin/python -m docproof inventory "Maiden in the Maze copy.docx"
```

### How to group

Group by **kind of judgment**, not by convenience:

- **Mechanical** types are objective — a word is misspelled or it isn't. They
  tolerate crowding well.
- **Sentence grammar** types need parsing but are still decidable. Related ones
  actively help each other: defining `comma_splice` and `run_on_sentence` in the
  same prompt sharpens the boundary between them, which is why they share a
  pass.
- **Judgment calls** are style-sensitive and fire most often on deliberate
  prose. Keeping them together keeps their generous do-not-flag lists from
  leaking onto types that should be strict.

Two constraints on ordering:

1. **Pass order is precision order.** The validator keeps the first edit to
   claim a span in a paragraph and rejects later overlapping ones
   (`rejected_overlap`). Objective types run first so they win collisions.
2. **A key may appear in only one category.** Config validation rejects
   duplicates. To read a category more than once, use its `passes:` (above), not
   the same key in two groups — duplicated keys would double every finding
   rather than dedupe the overlap.

Grouping costs something real: a model juggling ten definitions is less precise
than one hunting a single error class, and truncation now loses every type in
the group for that chunk, not just one. If a type's false-positive rate jumps
after you group it, split it back out — that's a one-line config change.

Override per run without touching config, using `+` to combine and `,` to
separate passes:

```bash
.venv/bin/python -m docproof review draft.docx --error-types "spelling+repeated_word,comma_splice" --max-chunks 1
```

## Writing an error type

Copy `config/error_types/_template.yaml` to `<key>.yaml`. The filename stem must
equal the `key` field or the registry raises at load.

**The do-not-flag list is the whole game.** In fiction especially, the failure
that costs you the user's trust isn't a missed typo — it's "correcting" a
character's name, a dialect spelling, or a deliberate fragment. Every type here
devotes more prose to what *not* to flag than to what to flag. Write the
do-not-flag list first, from the false positives you expect.

Required fields: `key`, `name`, `version`, `detection_prompt`, `fix_guidance`.
Strongly recommended: `confidence_guidance` and 3–5 `examples`, at least one of
which is a negative (`finding: null`).

Two rules the pipeline depends on, which every `detection_prompt` and
`fix_guidance` must reinforce:

- **`original_text` is the complete sentence, verbatim** — even when the error
  is one character. The validator anchors the quote by exact string match and
  then shrinks it to a minimal edit itself; a finding that quotes just the
  misspelled word still works, but a finding whose quote doesn't appear
  character-for-character in the paragraph is discarded as
  `rejected_no_anchor`.
- **One fix per finding.** `corrected_text` repeats the sentence changing only
  this type's error. A sentence with a typo and a splice produces two findings.

If two types could both claim an error, say so explicitly in *both* files, the
way `comma_splice` and `run_on_sentence` point at each other. Otherwise the
combined prompt will report it twice.

`confidence_guidance` defines what high/medium/low mean *for that type*, and it
carries real weight: `min_confidence` in config gates which findings become
tracked changes, and anything below the gate lands in the "Possibly
intentional" section of `summary.md` instead of being applied. Low confidence is
the right home for anything that might be voice.

## Checking a new type

1. `docproof inventory <file>` — confirms the type loads and shows the pass plan.
2. Run it alone against a chunk or two:
   ```bash
   .venv/bin/python -m docproof review draft.docx --error-types "your_key" --max-chunks 2 --min-confidence low
   ```
   `--min-confidence low` surfaces everything so you can see what it *would*
   have flagged.
3. Read `findings.json`. A pile of `rejected_no_anchor` means the quoting
   instructions aren't landing — the model is paraphrasing instead of copying.
   `stats_by_error_type` shows the per-type volume once several are enabled.
4. Add it to a group in `config/default.yaml` and re-run to confirm it still
   behaves next to its neighbours.

`tests/test_error_types.py` enforces the mechanical parts of the contract: every
YAML loads, keys match filenames, every enabled key exists, every type has a
do-not-flag list and calibration examples whose `original_text` quotes the
example paragraph verbatim.
