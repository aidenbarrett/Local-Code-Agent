#!/usr/bin/env bash
# Run one condition of the experiment, end to end, gating every step.
#
# Runs INSIDE WSL2 Ubuntu (Window B). The llama-server runs on Windows
# (Window A) and is reached at 127.0.0.1:8080 through mirrored networking.
#
#   bash run_experiment.sh /mnt/c/Users/<you>/Downloads/local-code-agent.zip nuc-llama-30b
#
# Controlled probe of one case:
#   bash run_experiment.sh <zip> nuc-llama-30b test-failure-diagnose
#
# One cell of the mechanism experiment:
#   CONDITION=control bash run_experiment.sh <zip> nuc-llama-30b
#   CONDITION=narrow  bash run_experiment.sh <zip> nuc-llama-30b
#   CONDITION=skill   bash run_experiment.sh <zip> nuc-llama-30b
#
# Each step is a gate. Nothing after a failed gate is reported as a run.

# pipefail: the eval is piped to tee, and without it a Python crash is masked
# by tee's exit status and the script goes on to print "done".
set -uo pipefail
ZIP="${1:?path to local-code-agent.zip}"
PROFILE="${2:-nuc-llama-30b}"
CASE="${3:-}"
CONDITION="${CONDITION:-skill}"
NOSKILL="${NOSKILL:-0}"
[ "$NOSKILL" = "1" ] && CONDITION="control"
case "$CONDITION" in skill|narrow|control) ;; *)
    echo "CONDITION must be skill, narrow or control (got '$CONDITION')"; exit 1 ;;
esac
DEST="$HOME/local-code-agent"
OUT="$HOME/experiment-runs"
mkdir -p "$OUT"

step() { printf '\n===== %s =====\n' "$1"; }
fail() {
    printf '\nGATE FAILED: %s\n' "$1"
    printf 'No completed run happened. Any JSON already in %s may be partial or\n' "$OUT"
    printf 'from an earlier run; use the run name and manifest together.\n'
    exit 1
}

# --- 0. toolchain -------------------------------------------------------------
step "0. toolchain"
missing=()
for t in git cmake ctest g++ python3 unzip curl; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
done
python3 -c 'import venv' 2>/dev/null || missing+=("python3-venv")
if [ ${#missing[@]} -gt 0 ]; then
    echo "missing: ${missing[*]}"
    echo "installing with apt (you will be asked for your password)..."
    sudo apt-get update -qq && sudo apt-get install -y -qq \
        git cmake build-essential python3 python3-venv python3-pip unzip curl \
        || fail "apt install"
fi
for t in git cmake ctest g++ python3 unzip curl; do
    printf '  %-8s %s\n' "$t" "$(command -v "$t")"
done
cmake --version | head -1
g++ --version | head -1

# --- 1. the code, on the Linux filesystem, not under /mnt/c ------------------
step "1. unpack to $DEST"
[ -f "$ZIP" ] || fail "zip not found at $ZIP"
rm -rf "$DEST"
tmp="$(mktemp -d)"
unzip -q "$ZIP" -d "$tmp" || fail "unzip"
mv "$tmp/local-code-agent" "$DEST" || fail "unexpected zip layout"
rm -rf "$tmp"
cd "$DEST" || fail "cd"
echo "unpacked: $(find local_agent -name '*.py' | wc -l) source files"

# This script is often copied out beside the package. Refuse drift: the
# launcher is part of the source hash because it decides what actually runs.
me="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
if [ -f "$DEST/measurement/run_experiment.sh" ] && [ "$me" != "$DEST/measurement/run_experiment.sh" ]; then
    if ! cmp -s "$me" "$DEST/measurement/run_experiment.sh"; then
        printf '\nSTALE LAUNCHER: %s differs from the one in the package.\n' "$me"
        printf 'Copy it out and rerun:\n  cp %s ~/run_experiment.sh\n' "$DEST/measurement/run_experiment.sh"
        exit 1
    fi
    echo "launcher matches the package"
fi

# --- 2. environment -----------------------------------------------------------
step "2. python environment"
python3 -m venv .venv || fail "venv"
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q --upgrade pip >/dev/null
pip install -q -e ".[dev]" || fail "pip install (needs network from WSL)"
python -c 'import openai; print("openai", openai.__version__)' || fail "openai import"

# --- 3. the agent works on THIS Linux before it meets a model -----------------
step "3. authoritative pytest on this machine"
testlog="$(mktemp)"
python -m pytest -q >"$testlog" 2>&1; rc=$?
tail -3 "$testlog"
if [ "$rc" -ne 0 ]; then
    echo "--- last 40 lines ---"; tail -40 "$testlog"
    rm -f "$testlog"; fail "pytest"
fi
rm -f "$testlog"

# --- 4. the server, from here -------------------------------------------------
step "4. server reachable from WSL"
health="$(curl -sS http://127.0.0.1:8080/health 2>&1)" || fail "curl /health: $health  (is Window A running? is mirrored networking on?)"
echo "/health: $health"
echo "$health" | grep -q '"status":"ok"' || fail "server not ready: $health"
echo "/v1/models: $(curl -sS http://127.0.0.1:8080/v1/models | python -c 'import sys,json; print([m["id"] for m in json.load(sys.stdin)["data"]])')"

# --- 5. doctor ----------------------------------------------------------------
step "5. doctor ($PROFILE)"
local-agent --repo benchmark_fixture/cpp_project --profile "$PROFILE" doctor || fail "doctor: the profile's model alias is not what the server is serving"

# --- 6. qualification: no suite on an unqualified profile --------------------
step "6. qualify ($PROFILE)"
python measurement/qualify_server.py --profile "$PROFILE" --json "$OUT/qualify-$PROFILE.json" \
    --dump-dir "$OUT/qualify-failures" || fail "qualification. Fix these before running the suite."

# Every run name carries its condition so one cell cannot overwrite another.
COND="-$CONDITION"
COND_FLAG="--condition $CONDITION"
if [ -n "$CASE" ]; then
    RUN="probe-$CASE-$PROFILE$COND"
else
    RUN="$PROFILE$COND"
fi
case "$RUN" in
    *-control|*-narrow|*-skill|*-control-*|*-narrow-*|*-skill-*) ;;
    *) echo "refusing run name '$RUN': it does not carry a condition"; exit 1 ;;
esac

# Stale artifacts are worse than missing artifacts. Clear only this exact run.
rm -rf "$OUT/$RUN.json" "$OUT/$RUN-transcripts" "$OUT/$RUN.log" "$OUT/$RUN-manifest.json"

# --- 6.5. observations that cannot be recovered after the run -----------------
step "6.5. immutable run manifest"
manifest_args=(
    python measurement/capture_run_manifest.py
    --profile "$PROFILE"
    --condition "$CONDITION"
    --out "$OUT/$RUN-manifest.json"
)
if [ -n "$CASE" ]; then
    manifest_args+=(--case "$CASE")
fi
"${manifest_args[@]}" || fail "run manifest capture"

# Refuse a package whose declared hashes do not describe the bytes being run.
python - "$OUT/$RUN-manifest.json" <<'PY' || fail "manifest instrument identity"
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
declared = p["instrument"]["declared"]
observed = p["instrument"]["observed"]
keys = ["source_sha256", "base_prompt_sha256"]
if "outcome_contract_sha256" in declared or "outcome_contract_sha256" in observed:
    keys.append("outcome_contract_sha256")
bad = [(k, declared.get(k), observed.get(k)) for k in keys if declared.get(k) != observed.get(k)]
if bad:
    for k, want, got in bad:
        print(f"{k}: declared {want}, observed {got}")
    raise SystemExit(1)
print("instrument identity matches manifest declaration")
PY

# Actual device may not be independently observable from an OpenAI-compatible
# HTTP endpoint. Never infer it from the requested profile. The manifest says
# UNOBSERVED unless the operator deliberately supplies LOCAL_AGENT_ACTUAL_DEVICE.
python - "$OUT/$RUN-manifest.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8"))
t = p["target"]
print("requested device:", t["requested_device"])
print("actual device:", t["actual_device"] or "UNOBSERVED", f"({t['actual_device_source']})")
if t["actual_device"] is None:
    print("NOTE: set LOCAL_AGENT_ACTUAL_DEVICE before a deployment comparison if the runtime can establish it.")
PY

# --- 7. the run ---------------------------------------------------------------
if [ -n "$CASE" ]; then
    step "7. CONTROLLED PROBE ($PROFILE, case $CASE, once)"
    echo "Transcript lands in $OUT/$RUN-transcripts/ whatever the outcome."
    python evaluation/run_evaluation.py --profile "$PROFILE" --label "$RUN" --case "$CASE" $COND_FLAG \
        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \
        2>&1 | tee "$OUT/$RUN.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "the probe exited non-zero; see $OUT/$RUN.log"
else
    step "7. END-TO-END RUN ($PROFILE, 10 pilot cases, once each)"
    echo "These ten tasks validate the comparison. They are not confirmatory evidence"
    echo "for a population-level procedure hypothesis; fresh held-out tasks do that."
    echo "Output: $OUT/$RUN.json"
    python evaluation/run_evaluation.py --profile "$PROFILE" --label "$RUN" $COND_FLAG \
        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \
        2>&1 | tee "$OUT/$RUN.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "the run exited non-zero; see $OUT/$RUN.log"
fi

step "done"
echo "ledger:     see above and $OUT/$RUN.json"
echo "manifest:   $OUT/$RUN-manifest.json"
echo "per case:   python -c 'import json;[print(r[\"case\"], r[\"outcome\"], r[\"validity\"], r[\"succeeded\"], r[\"scope_violation\"], r[\"tool_calls\"], r[\"elapsed_s\"]) for r in json.load(open(\"$OUT/$RUN.json\"))[\"rows\"]]'"
echo "send back:  $OUT/$RUN.json, $OUT/$RUN-manifest.json, $OUT/$RUN-transcripts/*.json and $OUT/qualify-$PROFILE.json"
