#!/usr/bin/env bash
# P0.2 — capture the eval baseline scorecard(s).
#
# This is the line every later accuracy phase is measured against. It makes a
# handful of PAID model calls (the whole 232-case corpus is ~12 API requests,
# a few cents per model), so it is a manual, opt-in step — not part of any
# automated run.
#
# Usage:
#   export OPENAI_API_KEY=sk-...        # for the production baseline (gpt-5.6-luna)
#   # optionally, a second vendor for the Phase 3 candidate comparison:
#   export ANTHROPIC_API_KEY=sk-ant-... # runs claude-haiku-4-5
#   export GEMINI_API_KEY=...           # runs gemini-3.1-flash-lite
#   ./eval/baselines/run_baseline.sh
#
# It runs each model whose key is present, then copies the scorecard the CLI
# writes to output/eval/ into eval/baselines/<model>_<date>.{json,md} so the
# baselines are committable side by side. Re-runnable; new dates don't clobber.
set -euo pipefail

cd "$(dirname "$0")/../.."
PY="${PY:-.venv/bin/python}"
DATE="$(date +%F)"
DEST="eval/baselines"

run_one() {
  local model="$1" keyname="$2"
  if [ -z "${!keyname:-}" ]; then
    echo "skip  $model  (\$$keyname not set)"
    return 0
  fi
  echo "=== $model ==="
  "$PY" -m docproof.__main__ eval --model "$model"
  # cmd_eval writes to <output_dir>/eval/scorecard.{json,md}; preserve it here.
  for ext in json md; do
    if [ -f "output/eval/scorecard.$ext" ]; then
      cp "output/eval/scorecard.$ext" "$DEST/${model}_${DATE}.$ext"
      echo "  -> $DEST/${model}_${DATE}.$ext"
    fi
  done
}

# The production baseline first — gpt-5.6-luna is what the app ships and DocWatch
# review jobs actually run. Then any candidate detectors for the Phase 3 grid.
run_one gpt-5.6-luna          OPENAI_API_KEY
run_one claude-haiku-4-5      ANTHROPIC_API_KEY
run_one gemini-3.1-flash-lite GEMINI_API_KEY

echo
echo "Done. Committed baselines live in $DEST/ — commit them with a message that"
echo "names each model's per-type recall and trap-FP rate (the line Phase 2+ beats)."
