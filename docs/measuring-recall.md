# Measuring a recall lever with a paired Johnson compare

The honest test of a recall feature is not "does it fire" but "does it *add*
located recall on a real manuscript without flooding false positives." The way
to get that number is a **paired run**: two identical reviews that differ in
exactly one setting, each scored against a human proofread. The difference
between the two scores is the feature's contribution — nothing else moved.

This runbook does it for the **LanguageTool** pass (`v0.25.0`), but the shape is
the same for any lever: make two configs that differ in one line, run both, and
`compare` each against the human.

## The pair

`config/_luna.yaml` and `config/_luna_lt.yaml` already differ by **exactly** the
`languagetool:` block and nothing else:

- **Arm A — `_luna.yaml`**: LanguageTool off (the baseline).
- **Arm B — `_luna_lt.yaml`**: `languagetool.enabled: true`.

Both use `gpt-5.6-luna` (an OpenAI model) in batch mode. So `B − A` is
LanguageTool's contribution, holding the manuscript, the human denominator, the
detector, glossary, and adjudication all fixed.

> Both arms have `rewrite` and `storysheet` **off**, which is deliberate:
> rewrite also targets the mechanical tail LanguageTool aims at, and the
> first-edit-wins validator would let rewrite mask LanguageTool's catches,
> *understating* the delta. Isolating LanguageTool on a quiet base gives its
> full standalone contribution — the number that decides graduation. To instead
> measure its *marginal* value on top of everything, turn `rewrite.enabled` and
> `storysheet.enabled` on in **both** configs and re-run the pair.

## Prerequisites

- **The OpenAI key in the environment.** The CLI reads `OPENAI_API_KEY` from the
  environment only (the Keychain is a web-app convenience). The key is already
  in your Keychain — load it for the shell session with:
  ```bash
  export OPENAI_API_KEY=$(python3 -c 'import keyring; print(keyring.get_password("docproof","openai"))')
  ```
- **Java — for Arm B only.** LanguageTool runs as a local Java server; the first
  run downloads its jar (~260 MB, cached). Arm A needs no Java.
  ```bash
  java -version   # if missing:  brew install openjdk
  ```
- Paths (adjust if yours differ):
  ```bash
  SRC="$HOME/Desktop/Atmosphere/Proofread Pairs/Johnson - Book 1.docx"
  HUMAN="$HOME/Desktop/Atmosphere/Proofread Pairs/Johnson - Book 1 Proofread.docx"
  ```

## Step 0 — preview the cost before spending anything

```bash
.venv/bin/python -m docproof inventory "$SRC" --config config/_luna.yaml
```
`inventory` ingests and chunks only (no API) and prints the arithmetic. For
Johnson Book 1 it reports 88 chunks / 6 passes / 528 API calls and:

```
Rough cost on gpt-5.6-luna: ~$0.65 now, ~$0.33 as a batch
```

So **~$0.33 per arm** in batch — about **$0.70 for the pair**. Arm B adds only
LanguageTool's light confirm calls on top (propose runs locally and free), a few
cents more.

## Step 1 — submit both arms (batch, 50% cheaper)

```bash
# Arm A — LanguageTool OFF
.venv/bin/python -m docproof submit "$SRC" --config config/_luna.yaml \
    --out output/johnson-measure-A

# Arm B — LanguageTool ON  (needs Java)
.venv/bin/python -m docproof submit "$SRC" --config config/_luna_lt.yaml \
    --out output/johnson-measure-B
```
Each `submit` prints a **job id** — a timestamp slug like
`20260811-143002-Johnson---Book-1-a1b2`. Note both (or list them anytime with
`docproof status`); you pass them to `collect` next.

## Step 2 — wait, then collect

Batch is **asynchronous** — results land in minutes to hours.

```bash
.venv/bin/python -m docproof status                      # all queued reviews
.venv/bin/python -m docproof collect "$JOB_A" --out output/johnson-measure-A
.venv/bin/python -m docproof collect "$JOB_B" --out output/johnson-measure-B
```
Each `collect` writes the tracked-changes `.docx` into its `--out`. Find it:
```bash
OUT_A=$(ls output/johnson-measure-A/*.docx | head -1)
OUT_B=$(ls output/johnson-measure-B/*.docx | head -1)
```

## Step 3 — compare each arm against the human

```bash
.venv/bin/python -m docproof compare "$HUMAN" "$OUT_A" \
    --label-b docproof-A --out output/johnson-measure-A/compare
.venv/bin/python -m docproof compare "$HUMAN" "$OUT_B" \
    --label-b docproof-B --out output/johnson-measure-B/compare
```

## Step 4 — read the scorecard

Each compare writes `compare/compare.json`. The fields that matter:

```
score_a_as_truth.located_recall               # of the human's edits, how many docproof located
score_a_as_truth.located_recall_with_queries  # ...counting margin queries as located too
score_a_as_truth.precision                    # of docproof's edits, how many the human also made
totals.only_b                                 # docproof edits the human did NOT make (FP proxy)
```

Because the pair shares one manuscript and one human denominator, compare the
two files **directly** — no channel decomposition needed for the delta:

| Signal | Meaning |
|---|---|
| **Δ located_recall = B − A** | LanguageTool's recall contribution |
| **Δ only_b = B − A** | extra edits LanguageTool introduced (its precision cost) |
| **precision A vs B** | did LanguageTool drag precision down |

### Graduation rule of thumb

LanguageTool **graduates to on-by-default** if Δ located_recall is meaningfully
positive **and** precision holds — i.e. Δ only_b is modest relative to the recall
it bought, and `precision` doesn't fall more than a point or two. A recall bump
paid for with a flood of `only_b` edits is the confirm valve being too loose, not
a win; tighten `languagetool.edit_confidence` (more goes to query) and re-run.

For the honest *core-mechanical* recall (substitution/insertion/capitalization
only, excluding house-style sweeps and out-of-scope copy edits), decompose
`compare.json` by channel with the private-eval harness in
`~/.docproof-private-eval/` — but for the paired **delta**, the raw
`located_recall` is directly comparable and enough to decide graduation.

## Applying this to other levers

Same recipe, different one-line diff:

- **`compound_sentence_comma`** (ships inert): add its key to the house-commas
  pass in a config's `error_types`, pair against the same config without it.
- **`rewrite`**, **`storysheet`**, etc.: flip the single `enabled` and pair.

One setting per pair. The moment two things move at once, the delta stops
meaning anything.
