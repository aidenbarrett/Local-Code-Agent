#!/usr/bin/env bash
# Slice 3: the first time a real model drives the real orchestrator.
#
# Runs INSIDE WSL2 Ubuntu (Window B). The llama-server runs on Windows
# (Window A) and is reached at 127.0.0.1:8080 through mirrored networking.
#
#   bash slice3.sh /mnt/c/Users/<you>/Downloads/local-code-agent.zip nuc-llama-30b
#
# Controlled probe of one case (decision 12), transcript captured:
#   bash slice3.sh <zip> nuc-llama-30b test-failure-diagnose
#
# One cell of the mechanism experiment:
#   CONDITION=control bash slice3.sh <zip> nuc-llama-30b
#   CONDITION=narrow  bash slice3.sh <zip> nuc-llama-30b
#   CONDITION=skill   bash slice3.sh <zip> nuc-llama-30b
#
# Each step is a gate. The script stops at the first one that fails and says
# which. Nothing here is clever; it is the blueprint's slice 3 typed out.

# pipefail: the eval is piped to tee, and without it a Python crash is masked
# by tee's exit status and the script goes on to print "done".
set -uo pipefail
ZIP="${1:?path to local-code-agent.zip}"
PROFILE="${2:-nuc-llama-30b}"
CASE="${3:-}"            # optional: run one case only, as a probe
# CONDITION selects which of the three cells this run measures:
#   skill    the skill body and the skill's toolset
#   narrow   the same toolset, no body: the effect of taking tools away
#   control  no body, every registered tool
# The catalogue is off in all three, so skill minus narrow is procedure alone.
CONDITION="${CONDITION:-skill}"
NOSKILL="${NOSKILL:-0}"  # older spelling of CONDITION=control
[ "$NOSKILL" = "1" ] && CONDITION="control"
case "$CONDITION" in skill|narrow|control) ;; *)
    echo "CONDITION must be skill, narrow or control (got '$CONDITION')"; exit 1 ;;
esac
DEST="$HOME/local-code-agent"
OUT="$HOME/slice3-out"
mkdir -p "$OUT"

step() { printf '\n===== %s =====\n' "$1"; }
fail() {
    printf '\nGATE FAILED: %s\n' "$1"
    printf 'No run happened. Any probe or suite JSON already in %s is from an\n' "$OUT"
    printf 'EARLIER run. Do not send it as this run.\n'
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
echo "unpacked: $(find src -name '*.py' | wc -l) source files"

# This script was copied out of a previous package and lives in $HOME. If it
# has drifted from the one inside the zip, the JSON would report the package
# hash while a different experiment actually ran.
me="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
if [ -f "$DEST/devtools/slice3.sh" ] && [ "$me" != "$DEST/devtools/slice3.sh" ]; then
    if ! cmp -s "$me" "$DEST/devtools/slice3.sh"; then
        printf '\nSTALE LAUNCHER: %s differs from the one in the package.\n' "$me"
        printf 'Copy it out and rerun:\n  cp %s ~/slice3.sh\n' "$DEST/devtools/slice3.sh"
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
step "3. test suite on this machine"
# Once, not twice. Piping to tail loses the exit status, so the suite used to
# be run a second time just to read it: double the time and double the exposure
# to a flake disagreeing with itself.
testlog="$(mktemp)"
if command -v pytest >/dev/null 2>&1; then
    pytest -q >"$testlog" 2>&1; rc=$?
    tail -3 "$testlog"
else
    python devtools/minipytest.py tests >"$testlog" 2>&1; rc=$?
    tail -1 "$testlog"
fi
if [ "$rc" -ne 0 ]; then
    echo "--- last 40 lines ---"; tail -40 "$testlog"
    rm -f "$testlog"; fail "test suite"
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
local-agent --repo fixtures/cpp_sandbox --profile "$PROFILE" doctor || fail "doctor: the profile's model alias is not what the server is serving"

# --- 6. qualification: no suite on an unqualified profile --------------------
step "6. qualify ($PROFILE)"
python devtools/qualify.py --profile "$PROFILE" --json "$OUT/qualify-$PROFILE.json" \
    --dump-dir "$OUT/qualify-failures" || fail "qualification. Fix these before running the suite."

# --- 7. the run ---------------------------------------------------------------
# Every condition is named, including skill. It used to be the empty suffix,
# so CONDITION=skill wrote slice3-<profile>.json, which is the exact filename
# of the historical frozen dataset, and the rm below would have deleted it
# before the run started. A filename must never be able to masquerade as, or
# destroy, a dataset from a different prompt generation.
COND="-$CONDITION"
COND_FLAG="--condition $CONDITION"

if [ -n "$CASE" ]; then
    RUN="probe-$CASE-$PROFILE$COND"
else
    RUN="slice3-$PROFILE$COND"
fi

# Stale results are worse than no results. If a gate above stops the script, or
# this run dies, the previous run's JSON and transcripts must not be sitting
# there under the same names looking current. One round trip was already spent
# analysing a leftover transcript as if it were fresh.
#
# The delete is scoped to a name that always carries the condition, so it can
# only ever remove this run's own leftovers.
case "$RUN" in
    *-control|*-narrow|*-skill|*-control-*|*-narrow-*|*-skill-*) ;;
    *) echo "refusing to clear '$RUN': the name does not carry a condition"; exit 1 ;;
esac
rm -rf "$OUT/$RUN.json" "$OUT/$RUN-transcripts" "$OUT/$RUN.log"

if [ -n "$CASE" ]; then
    step "7. CONTROLLED PROBE ($PROFILE, case $CASE, once)"
    echo "Transcript lands in $OUT/$RUN-transcripts/ whatever the outcome."
    python tests/evals/run_evals.py --profile "$PROFILE" --label "$RUN" --case "$CASE" $COND_FLAG \
        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \
        2>&1 | tee "$OUT/$RUN.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "the probe exited non-zero; see $OUT/$RUN.log"
else
    step "7. END-TO-END RUN ($PROFILE, 10 cases, 7 scenarios, once each)"
    echo "Allow an hour on the 30B. The measured full suite was 22 minutes with"
    echo "skills and 38 without, because the control takes more turns."
    echo "Output: $OUT/$RUN.json"
    python tests/evals/run_evals.py --profile "$PROFILE" --label "$RUN" $COND_FLAG \
        --out "$OUT/$RUN.json" --workdir "$HOME/local-agent-evals" \
        2>&1 | tee "$OUT/$RUN.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || fail "the run exited non-zero; see $OUT/$RUN.log"
fi

step "done"
echo "ledger:     see above and $OUT/$RUN.json"
echo "per case:   python -c 'import json;[print(r[\"case\"], r[\"outcome\"], r[\"validity\"], r[\"succeeded\"], r[\"scope_violation\"], r[\"tool_calls\"], r[\"elapsed_s\"]) for r in json.load(open(\"$OUT/$RUN.json\"))[\"rows\"]]'"
echo "send back:  $OUT/$RUN.json, $OUT/$RUN-transcripts/*.json (if any) and $OUT/qualify-$PROFILE.json"
