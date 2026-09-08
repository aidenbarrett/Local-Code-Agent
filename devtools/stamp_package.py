#!/usr/bin/env python3
"""Write PACKAGE.json so a shipped zip can name the commit it came from.

Run immediately before packaging. A zip without this still reports its
source_sha256, which is the hash that actually proves what ran; the commit is
the human-readable half.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from local_agent.provenance import source_sha256  # noqa: E402


def main() -> int:
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                            capture_output=True, text=True)
    payload = {
        "commit": rev.stdout.strip() or None,
        "dirty": bool(status.stdout.strip()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_sha256": source_sha256(),
    }
    (ROOT / "PACKAGE.json").write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
