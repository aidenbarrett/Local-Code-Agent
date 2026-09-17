#!/usr/bin/env bash
# Build the distributable zip.
#
# Packaging mechanics are deliberately outside the measured source surface.
set -euo pipefail
cd "$(dirname "$0")/../.."

OUT="${1:-local-code-agent.zip}"

if [ -n "$(git status --porcelain)" ]; then
    echo "working tree is dirty; commit or stash before packaging" >&2
    exit 1
fi

python internal/scripts/stamp_package.py
rm -f "$OUT"
git archive --format=zip --prefix=local-code-agent/ -o "$OUT" HEAD
python - "$OUT" <<'PY'
import sys, zipfile
out = sys.argv[1]
with zipfile.ZipFile(out, "a") as z:
    z.writestr("local-code-agent/PACKAGE.json", open("PACKAGE.json").read())
names = zipfile.ZipFile(out).namelist()
roots = {n.split("/")[0] for n in names}
assert roots == {"local-code-agent"}, f"bad zip layout: {roots}"
for required in ("local-code-agent/PACKAGE.json",
                 "local-code-agent/internal/measurement/run_experiment.sh",
                 "local-code-agent/pyproject.toml",
                 "local-code-agent/chat.ps1",
                 "local-code-agent/local-code-agent.ps1"):
    assert required in names, f"missing from zip: {required}"
print(f"{out}: {len(names)} entries")
PY
cat PACKAGE.json
