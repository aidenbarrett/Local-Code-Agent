#!/usr/bin/env python3
"""Capture immutable local identities for a stacked change candidate.

The destination, declared stack parent, and candidate are deliberately separate:
using one ambiguous "base" identity can hide a stale destination under a valid stack.
This local record is never CI or PR authorization.
"""

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


def _identity(prefix: str, ref: str) -> dict[str, str]:
    commit = resolve_commit(ref)
    return {
        f"{prefix}_ref": ref,
        f"{prefix}_commit_sha": commit,
        f"{prefix}_tree_sha": tree_for(commit),
    }


def build_record(
    destination_ref: str, parent_ref: str, candidate_ref: str
) -> dict[str, str]:
    return {
        "schema": "lca.change-readiness-evidence/2",
        **_identity("destination", destination_ref),
        **_identity("parent", parent_ref),
        **_identity("candidate", candidate_ref),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture exact destination, parent, and candidate identities."
    )
    parser.add_argument("--destination", required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--head", default="HEAD", dest="candidate")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        record = build_record(args.destination, args.parent, args.candidate)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        args.out.write_text(payload, encoding="utf-8")
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(f"REFUSED: could not capture readiness evidence: {exc}", file=sys.stderr)
        return 2
    print(
        f"Captured candidate {record['candidate_commit_sha']} / "
        f"{record['candidate_tree_sha']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
