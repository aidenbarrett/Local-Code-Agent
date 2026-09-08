#!/usr/bin/env bash
# Build the distributable zip.
#
# Deliberately NOT in measurement/. `measurement/*.sh` is inside the set hashed by
# source_sha256, and this script builds the package rather than being part of
# the instrument being measured. Putting it there would mean every change to
# the packaging mechanics produced a new experimental generation, which is
# nonsense.
#
# The layout matters and is the reason this exists rather than a remembered
# command line: the launcher unzips into a temp directory and moves
# `local-code-agent/` out of it, so a zip built without --prefix unpacks flat
# and fails the "unexpected zip layout" gate. That has happened.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-local-code-agent.zip}"

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is dirty; commit or stash before packaging" >&2
    exit 1
fi

python scripts/stamp_package.py
rm -f "$OUT"
git archive --format=zip --prefix=local-code-agent/ -o "$OUT" HEAD
python - "$OUT" <<'PY'
import sys, zipfile
out = sys.argv[1]
# PACKAGE.json is generated, not tracked, so git archive does not carry it.
with zipfile.ZipFile(out, "a") as z:
    z.writestr("local-code-agent/PACKAGE.json", open("PACKAGE.json").read())
names = zipfile.ZipFile(out).namelist()
roots = {n.split("/")[0] for n in names}
assert roots == {"local-code-agent"}, f"bad zip layout: {roots}"
for required in ("local-code-agent/PACKAGE.json",
                 "local-code-agent/measurement/run_experiment.sh",
                 "local-code-agent/pyproject.toml"):
    assert required in names, f"missing from zip: {required}"
print(f"{out}: {len(names)} entries")
PY
cat PACKAGE.json
