#!/usr/bin/env python3
"""Capture immutable local change identity for later CI/readiness comparison."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class EvidenceError(RuntimeError):
    pass


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    if proc.returncode != 0:
        raise EvidenceError(proc.stderr.strip() or proc.stdout.strip() or "git failed")
    return proc.stdout.strip()


def resolve_commit(ref: str) -> str:
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if len(sha) != 40:
        raise EvidenceError(f"invalid commit identity for {ref}")
    return sha


def tree_for(commit: str) -> str:
    sha = git("rev-parse", "--verify", f"{commit}^{{tree}}")
    if len(sha) != 40:
        raise EvidenceError(f"invalid tree identity for {commit}")
    return sha


def build_record(base_ref: str, head_ref: str) -> dict[str, str]:
    base_sha = resolve_commit(base_ref)
    head_sha = resolve_commit(head_ref)
    return {
        "schema": "lca.change-readiness-evidence/1",
        "base_ref": base_ref,
        "base_commit_sha": base_sha,
        "base_tree_sha": tree_for(base_sha),
        "head_ref": head_ref,
        "head_commit_sha": head_sha,
        "head_tree_sha": tree_for(head_sha),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture exact candidate/base identity.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        record = build_record(args.base, args.head)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        args.out.write_text(payload, encoding="utf-8")
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: could not capture readiness evidence: {exc}", file=sys.stderr)
        return 2
    print(f"Captured {record['head_commit_sha']} / {record['head_tree_sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
