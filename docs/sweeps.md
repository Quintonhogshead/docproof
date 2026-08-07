# The deterministic layer: sweeps and the spell scan

Two things run before any model pass, cost nothing, and make no API call.
Together they are docproof's answer to the line the Atmosphere proofreading
brief draws:

> Reading catches judgment; scripts catch patterns — do not use one for the
> other's job.

The brief then asks for something no reader can honestly give: a final match
count per rule. A read can only promise it tried. A sweep can prove it
finished.

## Sweeps

`docproof/sweeps.py`. Each sweep is one house rule expressed as text patterns.
A sweep produces ordinary `Finding` objects, so it rides the same validator,
reassembler and report as a model pass — it becomes a tracked change the same
way, and a person reviews it the same way.

Enable them in `config/default.yaml`:

```yaml
sweeps:
  - sweep_ellipsis
  - sweep_dash
  - sweep_stacked_punctuation
  - sweep_doubled_word
  - sweep_century
  - sweep_dialogue_tag
```

| sweep | what it fixes |
|---|---|
| `sweep_ellipsis` | `...` and `. . .` become `…`; the spacing around it follows `style.ellipsis` (default `nbsp`, the house convention: a non-breaking space before, a plain space after — see below) |
| `sweep_dash` | `--`/`---` become an en dash between numbers, an unspaced em dash elsewhere; a spaced en dash *or* a pre-existing spaced em dash closes up to an unspaced em dash |
| `sweep_stacked_punctuation` | `!!`, `?!`, `‽` collapse to one mark; a question stays a question |
| `sweep_doubled_word` | `the the` → `the`, minus the doublings that are real English |
| `sweep_century` | `20th century` → `twentieth century` |
| `sweep_dialogue_tag` | the brief's dialogue-tag table, every cell |

Order is precision order, exactly as with `error_types`: the validator gives
the first finding to claim a span the right to it, and sweeps run before every
model pass because a scripted rule is more certain than anything the model
reports about the same characters.

### Why they stay quiet

A sweep runs unsupervised over a whole manuscript, so a false positive is not
a missed catch — it is an edit nobody asked for. Every sweep skips what it
cannot decide:

- `sweep_dash` leaves single hyphens and correct tight en dashes (`1999–2005`)
  alone, and ignores a line that is nothing but dashes, because that is a
  scene divider.
- `sweep_doubled_word` leaves `had had`, `that that`, `very very` and anything
  with punctuation between the two words.
- `sweep_dialogue_tag` skips action beats (`"Wait here." She turned away.` is
  correct), skips reporting verbs that take an object (`She said it again.`),
  never lowercases `I`, and never touches a named subject — lowercasing a
  character's name would be far worse than missing a comma. Named subjects are
  the `dialogue_tag` error type's judgment call instead.

Because both the sweep and the error type can see a pronoun-subject dialogue
tag, the model sometimes reports one the sweep already fixed. The validator
rejects it as `rejected_overlap` and the summary says so. That redundancy is
deliberate: turning sweeps off must not turn the rule off.

### A house-style choice: ellipsis spacing

Some sweeps enforce a rule; one enforces a *preference*. How an ellipsis sits
against its neighbours is the press's call, not a grammar fact, so it is a
config knob — `style.ellipsis` in `config/default.yaml`:

- `nbsp` (default) — a non-breaking space before the ellipsis, so it never
  wraps away from the word it trails, and a plain space after: `Bad␣…` (the
  space before is non-breaking). This is the Atmosphere house convention, set
  out in the proofreading brief, so it is the default.
- `closed` — no space before the ellipsis, a plain space after only when a word
  follows: `I… guess`, `"I don't know…"`. For a manuscript or imprint that sets
  ellipses closed up instead of to the house convention.
- `space` — a plain space on both sides.

Only the leading space changes between modes; the glyph is always normalized to
`…` and the trailing space before the next word is the same in all three. In
every mode the sweep leaves an already-correct ellipsis alone, so it never
re-touches one that already follows the configured convention.

Note that the default deliberately re-spaces a bare `…` that lacks the
non-breaking space, because the brief calls that out by name: "Check pre-existing
… characters too — many manuscripts contain … without the required non-breaking
space before." A comparison against a human file that sets ellipses closed up
will therefore show these as disagreements — the sweep is applying house style,
not fighting it.

### The scripted-check report

`summary.md` and `findings.json` carry a line per sweep:

```
- **Ellipsis character and spacing** (`sweep_ellipsis`): 12 flagged, 12 applied, 0 remaining
```

*Flagged* is what the sweep found. *Applied* is what reached the document —
lower when an edit overlapped an earlier one. *Remaining* is what the sweep's
own patterns still match after its fixes; anything above zero means the sweep
is not idempotent and its rule is not fully executed, which is logged as an
error. This is the report the brief demands, and the reason a rule stated in
the brief but absent from the report can be assumed not to have run.

`docproof inventory` prints the same counts before you spend anything, so the
free corrections are visible up front.

## The spell scan

`docproof/spellscan.py`. A dictionary scan that **never corrects anything**.

Out-of-dictionary is evidence, not a verdict. Asked what `Kaelith` should have
been, the dictionary offers `Lilith`; for `Vorrenth` it offers `Torrent`. A
checker that acted on that would quietly rename a character — which is the
brief's own worst case. So the scan classifies and hands the evidence to the
model:

- **Words the author owns** — repeated, or capitalized mid-sentence. These
  become a do-not-flag list sent to every pass. This is the half that pays for
  itself: it attacks the exact false positive every error type is written to
  avoid, using document-specific evidence no static list could carry.
- **Words to look at** — used exactly once, not written as a name, not in the
  dictionary. Handed over as things to read, never as things to change.

```yaml
spellcheck:
  enabled: true
  dictionary: en_US
  min_occurrences: 2        # seen this often → a coined term, not a typo
  suggestion_limit: 25      # candidates given dictionary guesses (~0.25s each)
  allowlist: []
```

The scan reads the **whole** document even when the run covers a few selected
sections. It changes nothing, so reading more costs nothing — and a coined
name is only recognisable as the author's by being used across the manuscript.
Scanning one chapter would file its own hero's name as a suspected typo.

The vocabulary section is identical for every chunk, so it caches with the rest
of the system prompt and is billed once per pass. A clean manuscript produces
an empty section and pays nothing.

`spylls` supplies the dictionary offline — no network, which matters because
the tests must not reach one and the watcher runs unattended. Without the
package the scan is skipped with a warning and the passes run as before. Only
`en_US` ships today; the other variants arrive with the variant work.

## What this layer does not do

The two silent normalizations the brief describes — straight quotes to curly,
and multiple spaces to one — are **not** sweeps. They are untracked edits
applied to the package before ingest, which is a different mechanism and a
different phase. Sweeps only ever produce tracked changes.
