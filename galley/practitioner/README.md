# Running the practitioner

The practitioner is a headless Claude Code session whose working directory is
a **per-book workspace** seeded from this directory. DocProof is its tool; the
session drives the `docproof` CLI and (optionally) `galley.orchestrator` as
instruments.

## Workspace layout

```
workspace/<book-slug>/
  CLAUDE.md          <- copied from galley/practitioner/CLAUDE.md
  KNOBS.md           <- copied from galley/practitioner/KNOBS.md
  .claude/skills/    <- copied from galley/practitioner/skills/
  .claude/settings.local.json   (seeded once: Bash allowlist for docproof +
                                 grep/head/tail/wc/ls/cat runs/*; never
                                 overwritten)
  source/<book>.docx
  profile.json       (written by /profile)
  PLAN.md            (written by /draft-plan, human-approved at the gate)
  approval.json      (written by `docproof galley approve` from the plan)
  state.json         (the run state machine — `galley state`, advanced with
                      BOTH --source and --config at every stage)
  runs/              (each docproof run's --out dir; *.log slices)
  deliverable/       (final docx + letter.md + style-sheet.md)
```

Flush the workspace at intake of a new book. Persistent memory — the
`galley_memory.db` store (house rulings, precedents, authors) and the
calibration file — lives OUTSIDE the workspace and carries across books.

## Launch

```bash
BOOK=/path/to/Book.docx SLUG=book-slug ./launch.sh
```

`launch.sh` builds the workspace, copies the manual, KNOBS, and skills, seeds
the permission allowlist, drops the source in place, and starts a headless
session with the intake prompt. The session then follows the loop in
CLAUDE.md: profile → plan → **gate (it stops and waits for a human reply)** →
execute → audit → adjudicate → verify → certify → deliver.

### Key isolation (both launchers)

The brain runs on the Claude Max **subscription** (`claude setup-token` once,
then `export CLAUDE_CODE_OAUTH_TOKEN=<token>`); the launcher refuses to start
without that token, because the fallback is the API key — real dollars for
every brain turn. The API keys reach only the docproof sifter processes: the
launcher strips `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from the brain's
environment (`env -u`) and puts `~/galley-bin/docproof` first on PATH — a
wrapper that sources `~/.docproof-eval.env` and execs the venv's `docproof`
against the main checkout. The brain never sees a key; a sifter never runs
without one (it fails loudly instead). `DOCPROOF_CANDIDATE_APPLY=1` is
exported for this deployment only (candidate screening may apply; production
keeps the shadow floor).

## The phase loop (`~/galley-bin/galley-run.sh`)

One phase per lean `-p` session, so no session carries the whole book's
context (cache-read scales with context × turns). Each phase reads its inputs
from workspace files and advances the state machine.

| `PHASE=` | What the session does | Spend |
|---|---|---|
| `profile` | Intake: open the state machine, `/profile` + `/draft-plan`, write `profile.json` + `PLAN.md`, STOP at the plan gate | $0 |
| `approve` | From the human-approved `PLAN.md`: materialize the run config, print `galley routes`, write `approval.json` (`galley approve --budget`). Every later paid verb runs with `--approval approval.json` | $0 |
| `sweeps` | Author the bespoke sweeps, dry-run, gate on blast radius, `--apply` | $0 |
| `ladder` | The mechanical wave: `docproof review … --approval` to `runs/ladder/` | paid |
| `flights` | Copy-edit flights on the proofread text (`/flight-deck`, `--approval --budget`) | paid judge |
| `audit` | `/audit`: density table, sampled pages, hypotheses to `runs/audit.json`; recommend a wave-2, STOP for approval | one call |
| `reread` | Gated wave-2: targeted chapter × error-class re-reads from the audit's hypotheses, under the marginal-cost ceiling; refuses without recorded approval | paid |
| `verify` | `galley verify --context BRIEF --approval` (`--dry-run` first for the call count): re-reads every applied edit + proofreads the accepted text; nonzero on a flagged edit or a high-severity residual | paid |
| `certify` | `galley certify --approval --source --config`: the delivery gate; every check must PASS | $0 |
| `deliver` | Requires `runs/verify.log` passed AND `runs/certify.txt` PASSED, else stops; `/adjudicate` + `/merge-desk`, $0 rebuild, reject-all round trip, letter + style sheet to `deliverable/` | $0 |

`PROMPT="…"` overrides a phase's prompt; `MODEL`/`PERM` override the brain
model and permission mode.

## Two integration depths

1. **Session-as-brain (this directory).** The session plans and calls CLI
   verbs; the orchestrator's Python hooks are unused. Maximum judgment,
   human-gated. This is the mode the skills document.
2. **Hooks-as-brain (`galley/brain.py`).** `make_auditor`/`make_planner` give
   `run_galley` a mechanical audit→plan loop with no session at all — the app
   path uses this for T2+ jobs. Cheaper, bounded, no taste. The session may
   still be layered on top for the gates and the adjudication.
