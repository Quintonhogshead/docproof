#!/usr/bin/env bash
# Launch a per-book practitioner workspace and a headless Galley session.
#
#   BOOK=/path/to/Book.docx SLUG=book-slug [WORKROOT=~/galley-workspaces] ./launch.sh
#
# Builds the workspace (manual + skills + source), then starts a headless
# Claude Code session at the intake step. The session stops at the plan gate
# and waits for a human reply before any paid work beyond wave-1 defaults.
set -euo pipefail

BOOK="${BOOK:?set BOOK=/path/to/manuscript.docx}"
SLUG="${SLUG:?set SLUG=book-slug}"
WORKROOT="${WORKROOT:-$HOME/galley-workspaces}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$WORKROOT/$SLUG"

# Release candidate-screening Apply for THIS deployment only. The subsystem's
# global floor stays contained (CANDIDATE_APPLY_RELEASED = False), so production
# never mutates a document from the unvalidated lane; here the practitioner has
# opted the Galley loop in, and mechanical-wave sets candidate_screening.mode:
# apply. Unset this to fall the lane back to shadow.
export DOCPROOF_CANDIDATE_APPLY=1

mkdir -p "$WS/source" "$WS/runs" "$WS/deliverable" "$WS/.claude"
cp "$HERE/CLAUDE.md" "$WS/CLAUDE.md"
rm -rf "$WS/.claude/skills"
cp -R "$HERE/skills" "$WS/.claude/skills"
cp "$BOOK" "$WS/source/"

cd "$WS"
exec claude -p "Intake: a new manuscript is at source/$(basename "$BOOK"). \
Follow the loop in CLAUDE.md. Start with /profile, then /draft-plan, and STOP \
at the plan gate for approval." \
  --permission-mode acceptEdits
