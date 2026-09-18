#!/usr/bin/env python3
"""Write PACKAGE.json so a shipped zip can name the commit it came from."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = ROOT / "internal"
sys.path.insert(0, str(INTERNAL))

from local_agent.provenance import source_sha256  # noqa: E402


def _git(*args: str) -> str:
    """Return Git stdout or fail closed when repository provenance is unknown."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot establish Git provenance: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"cannot establish Git provenance: git {' '.join(args)} exited {proc.returncode}"
        )
    return proc.stdout


def main() -> int:
    try:
        commit = _git("rev-parse", "HEAD").strip()
        status = _git("status", "--porcelain")
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not commit:
        print("ERROR: cannot establish Git provenance: empty commit id", file=sys.stderr)
        return 2

    payload = {
        "commit": commit,
        "dirty": bool(status.strip()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_sha256": source_sha256(),
    }
    (ROOT / "PACKAGE.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
