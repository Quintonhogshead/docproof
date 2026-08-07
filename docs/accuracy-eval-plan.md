# Accuracy evaluation — the scorecard

Everything docproof does at the model stage is currently unmeasured. A pass runs,
findings land in tracked changes, and nobody can say whether a prompt change, a
model swap, or a new verification step made the corrections *better* or merely
*different*. "Dramatically increase accuracy" is unfalsifiable until there is a
number to move.

This document specifies that number and the harness that produces it. It is
Phase 0 of the accuracy work: the measurement layer every later phase —
per-pass model routing, an adversarial verifier, ensemble+debate — is steered
by. Build this first. Everything after it is a bet you can't price until this
exists.

## The one number that matters

The harness produces, per error type and overall:

- **precision** — of the corrections docproof made, how many were real errors
- **recall** — of the real errors present, how many docproof caught
- **F1** — the two combined
- **false-positive rate on deliberate prose** — the headline

The last one is the trust metric, and it earns top billing for the reason the
rest of the codebase already agrees on: the failure that costs an author's
trust isn't a missed typo, it's "correcting" a character's dialect, a coined
name, or a deliberate fragment (`docs/error-types.md`, `docproof/sweeps.py`,
`docproof/spellscan.py` all say this in their own words). A harness that only
measured recall would reward exactly the behaviour that loses users. So the
corpus is built to punish false positives specifically, and the scorecard puts
that rate where you can't miss it.

If a proposed change doesn't move one of these numbers, it doesn't belong in the
accuracy work. That is the whole point of building this before anything else.

## The corpus

This is 70% of the work. Two rules govern it.

### Held-out discipline

The calibration examples in `config/error_types/*.yaml` are shown to the model
inside its own prompt (`docproof/analyzer.py` renders them as `CALIBRATION
EXAMPLES`). Scoring against them is training on the test set — the model has
already been told the answer. The eval corpus must be **fresh cases the model
has never seen in a prompt**, and the loader asserts no case text collides with
any shipped calibration example, so leakage can't quietly creep back in as the
prompts evolve.

### Three tiers

Ground-truth quality differs by error class, so the corpus is sourced three
ways:

| Tier | Types | How cases are made | Why |
|---|---|---|---|
| **Injected** | spelling, repeated_word, homophone_confusion, apostrophe_error, capitalization | Take clean public-domain prose (Project Gutenberg), apply a *known* corruption programmatically. The expected correction is the exact inverse of the edit. | Ground truth is mechanically exact; cases are cheap to mass-produce. |
| **Authored** | tense_shift, missing_word, pronoun_agreement, comma_splice, run_on_sentence, subject_verb_agreement, dialogue_tag, the rest | Hand-written held-out paragraphs (LLM-drafted, human-verified) with a labeled span and expected fix. | Judgment errors can't be injected mechanically. They need real prose with a real ambiguity. |
| **Traps** | all types | Hand-curated deliberate prose: dialect dialogue, intentional fragments, coined fantasy names, stylistic comma choices. Labeled `clean`. | The adversarial set. Any finding here is a false positive. Must be human-authored — this is the set that defends the trust metric. |

The substrate is **public-domain prose plus hand-authored cases**: fully
shareable, no author-IP entanglement, committable to the repo. The tradeoff is
that none of it is Atmosphere house style verbatim; a later pass can add a
private, uncommitted tier drawn from real anonymized manuscripts if the
public corpus proves unrepresentative.

Target size: ~15–20 cases per type, roughly half genuine errors and half traps,
for ~400 cases total. Large enough for stable per-type numbers, small enough
that a full real-model run is a few dollars and can ride the batch API's 50%
discount.

### Case format

Modeled on the existing `examples:` block so it reads the same as the error-type
files:

```yaml
# eval/cases/tense_shift.yaml
error_type: tense_shift
cases:
  - id: ts-001
    text: "She locked the car and walks toward the diner."
    expect: { span: "walks", correction: "walked", confidence: high }
  - id: ts-002
    text: "She walks the same road she walked as a child."
    expect: clean          # a trap — flagging this is a false positive
```

`expect: clean` marks a trap. An `expect` block marks a seeded error: `span` is
the text that should be edited, `correction` the expected result (matched
exactly only as a secondary metric — a right-span/wrong-fix still counts as
caught), `confidence` the label the model ought to assign.

## Architecture

Four components, reusing the pipeline wholesale rather than reimplementing it.

1. **Corpus loader** — reads `eval/cases/*.yaml`, validates against a schema,
   runs the calibration-leakage check.
2. **Doc builder** — packs cases into one `.docx`, one case per paragraph, via
   the same `python-docx` path as `tests/fixtures/make_fixtures.py`. Records a
   `para_id → case_id` map (paragraph ids are deterministic, so the join is
   exact).
3. **Runner** — calls `prepare` + `run_sync` **at `min_confidence: low`** so
   nothing is gated away before scoring, and captures the analyzer's *raw*
   findings, not just post-validation `findings.json`. This is the only new hook
   into the pipeline: recall must be measured independent of the confidence
   gate.
4. **Scorer + report** — joins findings back to cases and computes the numbers.

## Matching rules

Where evals live or die. For each finding against each case:

- **True positive** — finding lands on an error case, its edited span overlaps
  the labeled span, and the error type matches (or is in the same family).
  Whether the *correction* also matched exactly is recorded as a secondary
  metric.
- **False negative** — an error case with no matching finding. A miss.
- **False positive** — any finding on a `clean` trap case. The trust number.
- **Spurious** — a finding on an error case but on the wrong span. Tracked
  apart from trap false positives; it's a different failure mode.

Because the run is at `min_confidence: low`, the scorer re-applies the
low/medium/high gate in post and emits a **precision/recall curve across
confidence thresholds from a single run**. That directly answers "should the
default gate be medium or high?" — one of the exact knobs the later phases tune.

## The scorecard

A `scorecard.json` (machine-diffable) and a `scorecard.md` (readable):

- Per error type: TP / FP / FN / precision / recall / F1.
- Overall micro and macro P/R/F1.
- **Trap false-positive rate** — front and center.
- **Anchor-failure rate** (`rejected_no_anchor` ÷ findings) — the "quoting
  instructions aren't landing" signal `docs/error-types.md` already prescribes
  reading by hand.
- P/R/F1 at each confidence threshold.
- Model id and total run cost, so two runs are comparable.

Comparing two models is running the same corpus under each `--model` and
diffing the scorecards. That is what makes every later phase measurable instead
of a guess.

## CLI

A subcommand parallel to `inventory` / `review`:

```bash
docproof eval --model claude-sonnet-5 --error-types tense_shift,missing_word
```

`--error-types` reuses the existing pass override so a single pass can be scored
cheaply while iterating on its prompt. No args runs the full corpus.

## Build order

1. Case schema + loader + leakage check.
2. ~40 injected + ~40 authored + ~40 traps — enough to prove the harness
   end-to-end before scaling the corpus.
3. Doc builder + `para_id` mapping.
4. Runner with the raw-findings hook.
5. Scorer + matching rules + threshold sweep.
6. `scorecard.md` writer + `docproof eval` CLI.
7. Scale the corpus to full size; run the current default model to capture the
   **baseline** every later phase is measured against.

Steps 1–6 are the engine. Step 7 is the first real accuracy number for today's
pipeline — and the line every later phase has to beat.

## What it costs

The corpus authoring is the real cost: human time on the traps and the authored
judgment cases, which can't be mechanized without becoming a model grading a
model. Running the harness is cheap — ~400 short paragraphs is a fraction of one
manuscript, so a full-model eval is a few dollars, and iterating on one pass is
cents.
