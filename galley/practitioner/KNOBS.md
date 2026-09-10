# KNOBS — selective reference index

Read only the contract needed for the current action. Do not load this directory
or historical KNOBS wholesale. The approved config is immutable: construct and
price before approval, reuse afterward. Prefer `genre-pack` materialization;
omitting `error_types` or `sweeps` from a hand-written config disables that work.

| Task | Reference |
|---|---|
| Stage/genre composition, full-config mechanics, approval | `references/config.md` |
| Full typed-pass/sweep lists, only when hand-building a config | `references/config-defaults.md` |
| A specific knob and default | matching row in `references/knobs.md` |
| Canonical paragraph IDs, zones, imports/replay | `references/findings.md` |
| A bespoke sweep and its output rows | `references/sweeps.md` |
| Narrow judgment subagent mandate | `references/judgment.md` |
| Settlement/owner-map field schema | `references/settlement-contracts.md` |
| Final finding-owned comments and internal repairs | `references/comment-reconciliation.md` |

Discover a verb's exact flags with `docproof capabilities galley verify` (replace
with that command path). A missing contract is a blocker to resolve; never read
`config.py`, `default.yaml`, `sweeps.py`, or the entire capability tree. Historical
behavior and all former guidance are preserved in `references/history/`; search
only for a specific precedent, never load it as current instructions.
