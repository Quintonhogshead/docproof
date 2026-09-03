#!/usr/bin/env bash
# Galley phase launcher — a THIN WRAPPER over `docproof galley drive`.
#
# The phase prompts, the workspace seeding, the key isolation and the plan gate
# all live in the repo now (galley/driver.py), so this script is only the
# familiar one-phase-at-a-time front door a person types:
#
#   BOOK="/path/Book.docx" SLUG=book-slug PHASE=ladder ./galley-run.sh
#
# Install it over ~/galley-bin/galley-run.sh; the old copy carried the prompts
# inline and drifted from the manual every time the loop changed.
#
# For a whole book unattended, skip this and run the driver directly:
#
#   docproof galley drive --book "$BOOK" --slug "$SLUG" \
#       --approve auto --budget 20 --drive-folder-id <folder>
#
# PROMPT="…" still overrides a phase's prompt entirely (it goes straight to
# `claude -p`). MODEL / PERM override the brain model and permission mode.
#
#   Prereq (one-time): claude setup-token  ->  export CLAUDE_CODE_OAUTH_TOKEN=…
set -euo pipefail

BOOK="${BOOK:?set BOOK=/path/to/manuscript.docx}"
SLUG="${SLUG:?set SLUG=book-slug}"
WORKROOT="${WORKROOT:-$HOME/galley-workspaces}"
WRAPBIN="${WRAPBIN:-$HOME/galley-bin}"
MODEL="${MODEL:-claude-fable-5}"
PERM="${PERM:-acceptEdits}"
PHASE="${PHASE:-profile}"
# Go-live scope: mechanical proofreading only. COPYEDIT=1 re-opens the
# `flights` / `reread` phases for an experiment.
COPYEDIT_FLAG=()
if [ -n "${COPYEDIT:-}" ]; then COPYEDIT_FLAG=(--copyedit); fi

if [ -n "${PROMPT:-}" ]; then
  # An explicit prompt bypasses the phase table; the driver still seeds the
  # workspace and builds the isolated environment.
  docproof galley drive --book "$BOOK" --slug "$SLUG" \
    --workspace-root "$WORKROOT" --dry-run >/dev/null
  cd "$WORKROOT/$SLUG"
  exec env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
       PATH="$WRAPBIN:$PATH" DOCPROOF_CANDIDATE_APPLY=1 \
       claude -p "$PROMPT" --model "$MODEL" --permission-mode "$PERM"
fi

# One phase, its own lean session; the plan gate stays a human's (--approve
# manual is today's behaviour, and the driver refuses to auto-approve here).
exec docproof galley drive \
  --book "$BOOK" --slug "$SLUG" --workspace-root "$WORKROOT" \
  --phases "$PHASE" --approve manual --model "$MODEL" \
  --permission-mode "$PERM" --wrapbin "$WRAPBIN" "${COPYEDIT_FLAG[@]}"
