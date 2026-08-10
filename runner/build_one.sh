#!/bin/zsh
# DeployEval model-pinned build driver.
# Usage: build_one.sh <task> <model-alias> <stack-name>
# Runs a headless, model-pinned Claude Code build agent and captures:
#   runner/out/<task>__<model>.json   full CLI result (incl. total_cost_usd + modelUsage from Bedrock)
#   runner/out/<task>__<model>.txt    the agent's final report text (for LIVE_URL parsing)
# NOTE: --dangerously-skip-permissions is used with the user's explicit authorization for these
# scoped build agents (personal IAM sandbox account, confined to the deployeval dir); headless
# `-p` mode cannot show interactive prompts, so this is required for autonomous deploy.
set -e
TASK="$1"; MODEL="$2"; STACK="$3"
# BASE is derived from this script's location (repo/runner/build_one.sh -> repo).
BASE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$BASE/runner/out"
RUNDIR="$BASE/runs/${TASK}-${MODEL}"
mkdir -p "$OUT" "$RUNDIR"

# CLAUDE_BIN: path to the Claude Code CLI. Override for your environment; defaults to `claude` on PATH.
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export AWS_PROFILE="${AWS_PROFILE:-default}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
# Amazon-internal wrappers expose a credential-export subcommand; harmless/no-op elsewhere.
"$CLAUDE_BIN" default-credential-export >/dev/null 2>&1 || true

# Prompt files use a {{BASE}} placeholder for portability; substitute the real repo path at runtime.
PROMPT="$(sed "s#{{BASE}}#${BASE}#g" "$BASE/runner/${TASK}.prompt.txt")
STACK_NAME to deploy (use this EXACT name): ${STACK}
Work in this directory (create if missing): ${RUNDIR}"

# --fallback-model pinned to the SAME target model: a rate-limit retry must NOT silently
# fall back to a different model (a global fallback of a stronger model would mislabel trials).
"$CLAUDE_BIN" -p "$PROMPT" \
  --model "$MODEL" \
  --fallback-model "$MODEL" \
  --output-format json \
  --add-dir "$BASE" \
  --dangerously-skip-permissions \
  > "$OUT/${TASK}__${MODEL}.json" 2> "$OUT/${TASK}__${MODEL}.err" || true

# Provenance guard: verify the build actually ran on the TARGET model (not a fallback).
python3 - "$OUT/${TASK}__${MODEL}.json" "$OUT/${TASK}__${MODEL}.txt" "$MODEL" <<'PY'
import json, sys
src, dst, target = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(src))
    open(dst, "w").write(d.get("result", "") or "")
    mu = d.get("modelUsage", {})
    keys = list(mu.keys())
    print("MODEL_USAGE_KEYS:", keys)
    print("TOTAL_COST_USD:", d.get("total_cost_usd"))
    print("IS_ERROR:", d.get("is_error"))
    # canonicalModel is the ground-truth identity Bedrock reports
    canon = {mu[k].get("canonicalModel", k) for k in keys}
    if keys == [] :
        print("PROVENANCE: NO_USAGE (build never ran the model)")
    elif keys == [target] or canon == {target}:
        print("PROVENANCE: OK", target)
    else:
        print("PROVENANCE: MISMATCH target=", target, "actual=", keys)
except Exception as e:
    open(dst, "w").write("")
    print("PARSE_ERROR:", e)
PY
echo "BUILD_DONE ${TASK} ${MODEL} -> ${STACK}"
