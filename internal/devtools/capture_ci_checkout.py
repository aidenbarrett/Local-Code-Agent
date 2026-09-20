#!/usr/bin/env python3
"""Record the exact GitHub Actions checkout identity without inferring PR state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout.strip()


def record(env: dict[str, str]) -> dict[str, str | None]:
    checkout = git("rev-parse", "--verify", "HEAD^{commit}")
    tree = git("rev-parse", "--verify", "HEAD^{tree}")
    github_sha = env.get("GITHUB_SHA")
    if github_sha and github_sha != checkout:
        raise RuntimeError("GITHUB_SHA does not match the checked-out commit")
    return {
        "schema": "lca.ci-checkout-evidence/1",
        "event_name": env.get("GITHUB_EVENT_NAME"),
        "checkout_commit_sha": checkout,
        "checkout_tree_sha": tree,
        "github_sha": github_sha,
        "pr_head_sha": env.get("LCA_PR_HEAD_SHA") or None,
        "pr_base_sha": env.get("LCA_PR_BASE_SHA") or None,
    }


def main() -> int:
    try:
        value = record(dict(os.environ))
        out = Path(os.environ.get("LCA_CHECKOUT_EVIDENCE", "ci-checkout-evidence.json"))
        out.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: checkout identity unavailable: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
