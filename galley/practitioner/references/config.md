# Config preparation and approval

Read for profile/planning or approval. After approval, **reuse the exact approved
config unchanged**. Do not regenerate, rewrite, or substitute it in ladder or a
later phase; changed source/config/routes require a new approval. A separate
final-replay config may describe the engine's authorized rebuild; it never
replaces or changes the frozen paid run config.

Materialize the full config with `docproof galley genre-pack` before approval,
then apply only the planned overlays and inspect `docproof galley routes` and
the exact command's `--dry-run`. The dry-run estimate is authoritative over
historical rates; price each approved paid lane, including 15% headroom. Keep
all required lanes and the approved API ceiling (phase prompt, default $10).
Sapling is never a default whole-book lane: it needs an explicit character budget
and confirmed availability; a paid detector reporting $0 is a coverage warning.

Look up only a named knob's row in `references/knobs.md`; load
`references/config-defaults.md` only to construct an explicitly necessary full
config. Do not read source or the full default YAML. A missing knob is a block
to resolve, not permission to invent a setting. Materialized YAML lists at column
zero below their key are valid; do not mistake them for empty sections.

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
discipline), so the full lists are in `references/config-defaults.md` when a
hand-written config is necessary. Prefer materializing with `genre-pack`. Never assume "leave it out = use defaults"; leave it out = OFF.

`--profile`, `--stage`, and genre packs are post-load overlays (they layer on
top). `error_type_override_dir` shadows shipped detector prompts by key.

## Stages, genres, and the approval gate (post-load overlays)

Three separate axes compose onto a base config. Precedence, strict-to-loose:
**profile > stage > genre > base.**

- **`--stage`** (`config/stages/`) chooses *which lanes run* and LOCKS some so a
  genre cannot reopen them. `mechanical-wave` is the portable Wave 1 recipe:
  the ensemble block in this reference, over the base's full typed passes/sweeps, repair on,
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
    - {model: claude-sonnet-5, effort: low}    # diverse union = recall ($0: subscription lane)
  verifier_model: gpt-5.6-luna                  # precision over the disputed set
  verifier_effort: high
  verify_policy: disputed
```

`ensemble` fires ONLY through `error_types` — pair it with the full typed-pass
list, never alone (see the trap above).
