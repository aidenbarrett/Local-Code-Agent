from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

_SHA = re.compile(r"Reconciled against GitHub `main` at `([0-9a-fA-F]{40})`")


def recorded_sha(text: str) -> str:
    match = _SHA.search(text)
    if match is None:
        raise ValueError("CURRENT_STATE.md must contain one full reconciliation SHA")
    return match.group(1).lower()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
    return completed.stdout.strip()


def check(root: Path, *, target: str = "HEAD", max_behind: int = 5) -> int:
    if max_behind < 0:
        raise ValueError("max_behind must be non-negative")
    recorded = recorded_sha((root / "CURRENT_STATE.md").read_text(encoding="utf-8"))
    resolved_target = _git(root, "rev-parse", target)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", recorded, resolved_target],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if ancestor.returncode == 1:
        raise RuntimeError(f"CURRENT_STATE reconciliation {recorded} is not an ancestor of {resolved_target}")
    if ancestor.returncode != 0:
        raise RuntimeError(ancestor.stderr.strip() or "could not validate reconciliation ancestry")
    behind = int(_git(root, "rev-list", "--first-parent", "--count", f"{recorded}..{resolved_target}"))
    if behind > max_behind:
        raise RuntimeError(
            f"CURRENT_STATE.md is {behind} first-parent commits behind {resolved_target}; "
            f"allowed drift is {max_behind}. Reconcile the document against current main."
        )
    return behind


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when CURRENT_STATE.md drifts too far from source.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--max-behind", type=int, default=5)
    args = parser.parse_args()
    behind = check(args.root.resolve(), target=args.target, max_behind=args.max_behind)
    print(f"CURRENT_STATE reconciliation is {behind} first-parent commit(s) behind {args.target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
