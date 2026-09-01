#!/usr/bin/env bash
# Launch a per-book practitioner workspace and a headless Galley session.
#
#   BOOK=/path/to/Book.docx SLUG=book-slug [WORKROOT=~/galley-workspaces] ./launch.sh
#
# Builds the workspace (manual + knobs + skills + source), then starts a
# headless Claude Code session at the intake step. The session stops at the
# plan gate and waits for a human reply before any paid work beyond wave-1
# defaults. For the phase-at-a-time loop (profile → approve → sweeps → ladder →
# flights → audit → reread → verify → certify → deliver) see README.md and
# galley-bin/galley-run.sh — this launcher runs the intake phase only.
#
#   Prereq (one-time): claude setup-token   -> gives CLAUDE_CODE_OAUTH_TOKEN
#   export CLAUDE_CODE_OAUTH_TOKEN=<token>
set -euo pipefail

BOOK="${BOOK:?set BOOK=/path/to/manuscript.docx}"
SLUG="${SLUG:?set SLUG=book-slug}"
WORKROOT="${WORKROOT:-$HOME/galley-workspaces}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$WORKROOT/$SLUG"
# The `docproof` wrapper that re-injects the API keys for the sifter processes
# only (see galley-bin/docproof): the brain never sees them.
WRAPBIN="${WRAPBIN:-$HOME/galley-bin}"
MODEL="${MODEL:-claude-fable-5}"
PERM="${PERM:-acceptEdits}"

# Release candidate-screening Apply for THIS deployment only. The subsystem's
# global floor stays contained (CANDIDATE_APPLY_RELEASED = False), so production
# never mutates a document from the unvalidated lane; here the practitioner has
# opted the Galley loop in, and mechanical-wave sets candidate_screening.mode:
# apply. Unset this to fall the lane back to shadow.
export DOCPROOF_CANDIDATE_APPLY=1

# --- Auth guard: the brain must run on the subscription, not API dollars ------
# Strip any whitespace/newlines a paste may have injected into the token
# (a wrapped paste puts a line break mid-token -> "Invalid Authorization header").
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN" | tr -d '[:space:]')"
fi
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "ERROR: CLAUDE_CODE_OAUTH_TOKEN is not set — the brain would fall back to" >&2
  echo "the ANTHROPIC_API_KEY (API dollars) or fail as 'not logged in'." >&2
  echo "Fix: run 'claude setup-token' once, then: export CLAUDE_CODE_OAUTH_TOKEN=<token>" >&2
  exit 3
fi
if [ ! -x "$WRAPBIN/docproof" ]; then
  echo "ERROR: no docproof wrapper at $WRAPBIN/docproof — the sifters would have" >&2
  echo "no keys (or the brain would). Install galley-bin/docproof, or set WRAPBIN=." >&2
  exit 3
fi

# --- Build / refresh workspace (idempotent; preserves settings.local.json & runs/) ---
mkdir -p "$WS/source" "$WS/runs" "$WS/deliverable" "$WS/.claude"
cp "$HERE/CLAUDE.md" "$WS/CLAUDE.md"
cp "$HERE/KNOBS.md"  "$WS/KNOBS.md"
rm -rf "$WS/.claude/skills"
cp -R "$HERE/skills" "$WS/.claude/skills"
cp "$BOOK" "$WS/source/"

# Seed a Bash allowlist for the rack verbs and the read-only slicing tools the
# skills lean on, so a headless session is not stalled on permission prompts.
# Never overwrite one that exists — a workspace's own edits win.
SETTINGS="$WS/.claude/settings.local.json"
if [ ! -f "$SETTINGS" ]; then
  cat > "$SETTINGS" <<'JSON'
{
  "permissions": {
    "allow": [
      "Bash(docproof:*)",
      "Bash(grep:*)",
      "Bash(head:*)",
      "Bash(tail:*)",
      "Bash(wc:*)",
      "Bash(ls:*)",
      "Bash(cat runs/:*)"
    ]
  }
}
JSON
fi

cd "$WS"
# Withhold API keys from the brain (subscription OAuth); the wrapper on PATH
# re-injects keys + the checkout's code for the docproof sifters only.
exec env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
     PATH="$WRAPBIN:$PATH" \
     CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
     claude -p "Intake: a new manuscript is at source/$(basename "$BOOK"). \
Follow the loop in CLAUDE.md. Start with /profile, then /draft-plan, and STOP \
at the plan gate for approval." \
  --model "$MODEL" --permission-mode "$PERM"
