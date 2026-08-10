#!/usr/bin/env bash
# P2.5 — the ensemble gate matrix. Scores three configurations on the full
# corpus so the Phase 2 go/no-go is a scorecard diff, not a guess:
#
#   1. baseline   — single detector (already captured in P0.2; re-run for a
#                   clean same-day comparison)
#   2. union2     — two detectors, findings merged, NO verifier
#   3. verified2  — two detectors + a stronger overseer-verifier on disputed
#
# PAID: this is 2-3x a single review per config, plus the verifier. Still a few
# cents each on cheap detectors. Needs both detector vendors' keys (and the
# verifier's):
#
#   export OPENAI_API_KEY=sk-...        # gpt-5.6-luna detector
#   export ANTHROPIC_API_KEY=sk-ant-... # claude-haiku-4-5 detector + opus verifier
#   ./eval/baselines/run_ensemble_matrix.sh
#
# Gate (from the plan): micro recall up >= 5 pts OR worst-5-type recall up >= 10
# pts vs the post-P1 baseline, AND trap-FP within the <1 pt noise band. If
# union2 alone clears it, ship that and skip the verifier. NOTE: fix the scorer
# to credit sweep-owned types (repeated_word, dialogue_tag) BEFORE trusting a
# per-type recall diff on those — see the memory note.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PY:-.venv/bin/python}"
DATE="$(date +%F)"
DEST="eval/baselines"
VERIFIER="${VERIFIER:-claude-opus-5}"

if [ -z "${OPENAI_API_KEY:-}" ] || [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Need both OPENAI_API_KEY and ANTHROPIC_API_KEY (detectors span both vendors)."
  echo "Set them and re-run. Nothing spent."
  exit 0
fi

# Write a patched config: default.yaml + an ensemble section. Keeps the config
# in one place instead of committing a near-duplicate of default.yaml.
patch_config() {   # $1 = out path, $2 = python dict for cfg['ensemble']
  "$PY" - "$1" "$2" <<'PYEOF'
import sys, yaml
out, ensemble = sys.argv[1], eval(sys.argv[2])
cfg = yaml.safe_load(open("config/default.yaml"))
cfg["ensemble"] = ensemble
yaml.safe_dump(cfg, open(out, "w"), sort_keys=False)
PYEOF
}

run_cfg() {        # $1 = label, $2 = config path, $3 = model for the cost line
  echo "=== $1 ==="
  "$PY" -m docproof.__main__ eval --config "$2" --model "$3"
  for ext in json md; do
    cp "output/eval/scorecard.$ext" "$DEST/ensemble-${1}_${DATE}.$ext"
  done
  echo "  -> $DEST/ensemble-${1}_${DATE}.{json,md}"
}

mkdir -p output
DET="[{'model':'gpt-5.6-luna'},{'model':'claude-haiku-4-5'}]"

# 1. baseline (single detector) — default.yaml as shipped
run_cfg baseline config/default.yaml gpt-5.6-luna

# 2. union2 — two detectors, no verifier
patch_config output/eval_union2.yaml \
  "{'detectors':$DET,'verifier_model':None,'verify_policy':'none','verifier_effort':'high','consensus_confidence_bump':False}"
run_cfg union2 output/eval_union2.yaml gpt-5.6-luna

# 3. verified2 — two detectors + overseer-verifier on disputed findings
patch_config output/eval_verified2.yaml \
  "{'detectors':$DET,'verifier_model':'$VERIFIER','verify_policy':'disputed','verifier_effort':'high','consensus_confidence_bump':False}"
run_cfg verified2 output/eval_verified2.yaml gpt-5.6-luna

echo
echo "Done. Diff the three scorecards in $DEST/ against the P0.2 baseline and"
echo "record the go/no-go (recall delta vs trap-FP) in the commit message."
