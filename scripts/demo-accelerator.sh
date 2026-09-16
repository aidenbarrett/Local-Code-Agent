#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-NPU}"
SECONDS_PER_DEVICE="${2:-45}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

exec "$PYTHON" "$SCRIPT_DIR/demo-accelerator.py" --device "$DEVICE" --seconds "$SECONDS_PER_DEVICE"
