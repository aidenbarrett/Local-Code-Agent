#!/usr/bin/env python3
"""Fail-closed readiness checks for a committed change candidate.

Unlike finalize_change.py this command never mutates the tree. It proves that the
candidate is a committed descendant of the intended destination and that frozen
contract identities still match the trusted destination declaration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INSTRUMENT = ROOT / "internal" / "INSTRUMENT.json"
CONTRACT_KEYS = ("base_prompt_sha256", "outcome_contract_sha256")


class ReadinessError(RuntimeError):
    pass


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "git command failed"
        raise ReadinessError(detail)
    return proc.stdout.strip()


def resolve(ref: str) -> str:
    value = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if len(value) != 40:
        raise ReadinessError(f"could not resolve commit ref: {ref}")
    return value


def require_clean() -> None:
    if git("status", "--porcelain"):
        raise ReadinessError("working tree is dirty; readiness applies to committed bytes only")


def is_ancestor(base_sha: str, head_sha: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ReadinessError(proc.stderr.strip() or "could not establish ancestry")


def read_json_at(commit: str, path: str) -> dict[str, Any]:
    raw = git("show", f"{commit}:{path}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReadinessError(f"invalid JSON at {commit}:{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{commit}:{path} is not a JSON object")
    return value


def contract_drift(
    trusted: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    return {
        key: (trusted.get(key), candidate.get(key))
        for key in CONTRACT_KEYS
        if trusted.get(key) != candidate.get(key)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check committed PR/change readiness.")
    parser.add_argument("--base", required=True, help="intended destination ref")
    parser.add_argument("--head", default="HEAD", help="candidate ref (default: HEAD)")
    args = parser.parse_args(argv)

    try:
        require_clean()
        base_sha = resolve(args.base)
        head_sha = resolve(args.head)
        if base_sha == head_sha:
            raise ReadinessError("candidate contains no commits beyond destination")
        if not is_ancestor(base_sha, head_sha):
            raise ReadinessError(
                "candidate is not a descendant of the intended destination; "
                "refresh/rebase the stack before review"
            )
        trusted = read_json_at(base_sha, "internal/INSTRUMENT.json")
        candidate = read_json_at(head_sha, "internal/INSTRUMENT.json")
        drift = contract_drift(trusted, candidate)
        if drift:
            detail = ", ".join(sorted(drift))
            raise ReadinessError(
                "frozen contract identity differs from destination: " + detail
            )
    except (ReadinessError, OSError, subprocess.SubprocessError) as exc:
        print(f"NOT READY: {exc}", file=sys.stderr)
        return 2

    print(f"READY-CANDIDATE base={base_sha} head={head_sha}")
    print("Frozen contract axes match the intended destination.")
    print("This is a local structural gate, not CI or merge authorization.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
