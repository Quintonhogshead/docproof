# Running the practitioner

The practitioner is a headless Claude Code session whose working directory is
a **per-book workspace** seeded from this directory. DocProof is its tool; the
session drives the `docproof` CLI and (optionally) `galley.orchestrator` as
instruments.

## Workspace layout

```
workspace/<book-slug>/
  CLAUDE.md          <- copied from galley/practitioner/CLAUDE.md
  .claude/skills/    <- copied from galley/practitioner/skills/
  source/<book>.docx
  profile.json       (written by /profile)
  plan.md            (written by /draft-plan, human-approved at the gate)
  runs/              (each docproof run's --out dir)
  deliverable/       (final docx + letter.md + style-sheet.md)
```

Flush the workspace at intake of a new book. Persistent memory — the
`galley_memory.db` store (house rulings, precedents, authors) and the
calibration file — lives OUTSIDE the workspace and carries across books.

## Launch

```bash
BOOK=/path/to/Book.docx SLUG=book-slug ./launch.sh
```

`launch.sh` builds the workspace, copies the manual and skills, drops the
source in place, and starts a headless session with the intake prompt. The
session then follows the loop in CLAUDE.md: profile → plan → **gate (it stops
and waits for a human reply)** → execute → audit → adjudicate → deliver.

Environment: the session needs the docproof venv on PATH and the API keys
(`~/.docproof-eval.env` locally; Fly secrets in production). The plan gate is
enforced by instruction, and doubly by budget: launch with a low
`--max-spend`-style permission profile until the plan is approved.

## Two integration depths

1. **Session-as-brain (this directory).** The session plans and calls CLI
   verbs; the orchestrator's Python hooks are unused. Maximum judgment,
   human-gated. This is the mode the skills document.
2. **Hooks-as-brain (`galley/brain.py`).** `make_auditor`/`make_planner` give
   `run_galley` a mechanical audit→plan loop with no session at all — the app
   path uses this for T2+ jobs. Cheaper, bounded, no taste. The session may
   still be layered on top for the gates and the adjudication.
