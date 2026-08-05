# English variants

Most of the house style is the same everywhere. A handful of conventions flip,
and getting one wrong is worse than not supporting variants at all: a U.K.
manuscript reviewed as U.S. comes back Americanized, with every change looking
deliberate.

```yaml
variant: us     # us | uk | ca | au
```

**Stated, never inferred.** There is no auto-detection, deliberately. Guessing
the variant from spelling patterns and then applying the guess silently is
exactly how a book comes back in the wrong English, and the guess would be
invisible in the output.

## What each variant is

| | primary quote | dictionary | authorities |
|---|---|---|---|
| `us` | double `“…”` | en_US | Chicago 17 + Merriam-Webster |
| `uk` | single `‘…’` | en_GB | Oxford Style Manual + Oxford Dictionary for Writers and Editors |
| `ca` | double `“…”` | en_CA | Chicago 17 punctuation + Canadian Oxford spelling |
| `au` | single `‘…’` | en_AU | Oxford Style Manual + Macquarie |

Canadian and Australian are **hybrids**, and their change logs ask the press to
confirm the choice before the pass is treated as final. Canadian is the trap:
U.K.-looking spellings (`colour`, `centre`) with U.S. punctuation (`Mr.`,
commas inside quotes). Carrying the punctuation across with the spellings is
the usual way to get it wrong.

Each variant is one file in `config/variants/`, holding both the prose the
model is given and the parameters the scripted stages read. Four copies of every
error type would have drifted — fix a do-not-flag list in the U.S. file and the
other three quietly keep the bug.

## What actually changes

**The model passes.** The variant's conventions go into every pass's system
prompt, before the error-type sections, and say so: they override any
convention a type states in its own wording. That is what lets `that_which`
state the Chicago rule plainly and still behave correctly on a U.K. manuscript,
where Oxford permits either and the author's habit is what matters.

**The dialogue-tag sweep.** It keys off the closing quotation mark, so a
pattern hard-coded to double quotes would run over a U.K. book and report zero
matches — which reads exactly like a clean manuscript. The pattern is built per
variant.

**The normalizer.** This is the subtlest one. A word-initial straight quote is
ambiguous: `'tis` is a dialect elision, `'Hello,'` is dialogue opening. In a
double-quote variant the elision reading is more likely, so the mark is only
curled when the closing half is found nearby; in a single-quote variant it is
usually dialogue, so it curls. Elisions are recognised in both, because `'Tis`
is an apostrophe wherever it appears.

**The spell scan** picks the variant's dictionary, and **the change log**
states the variant and its authorities, with the confirmation request for the
hybrids.

## Dictionaries

`spylls` bundles `en_US` only. A `uk`, `ca` or `au` run therefore has **no
dictionary scan** unless you supply one:

```yaml
spellcheck:
  dictionary: /path/to/en_GB      # an .aff/.dic pair, without the extension
```

Rather than quietly checking British spelling against an American word list —
which would file every correct `colour` and `organise` as an unknown word, and
hand the model a "words to look at" list made entirely of correct spellings —
the scan declines to run, logs why, and the change log records it under limits.

The rest of the pass is unaffected: sweeps, model passes, normalization and the
audit all run normally. What is lost is the manuscript's own vocabulary as a
do-not-flag list, so invented names may be flagged as misspellings more often.

## Adding a variant

Write `config/variants/<key>.yaml` with `key`, `name`, `dictionary`,
`primary_quote`, `authorities` and `conventions`, then add the key to the
`Literal` in `Config.variant`. Nothing else needs to change — the sweeps, the
normalizer, the spell scan and the change log all read the variant rather than
switching on its name.

For a rule too specific to state in prose, `error_type_override_dir` still
shadows individual error types by key, which is the escape hatch when a variant
needs a genuinely different do-not-flag list rather than a different
convention.
