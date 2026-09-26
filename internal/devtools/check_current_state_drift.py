"""Fail CI when CURRENT_STATE.md drifts too far behind the tree it describes."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

_RECONCILED = re.compile(r"^Reconciled against GitHub `main` at `([0-9a-f]{40})`$", re.MULTILINE)


def recorded_sha(text: str) -> str:
    match = _RECONCILED.search(text)
    if match is None:
        raise ValueError("CURRENT_STATE.md must contain one full reconciliation SHA")
    return match.group(1)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def first_parent_distance(root: Path, recorded: str, target: str) -> int:
    if _git(root, "merge-base", "--is-ancestor", recorded, target) != "":
        # merge-base --is-ancestor succeeds with no output.
        pass
    count = _git(root, "rev-list", "--first-parent", "--count", f"{recorded}..{target}")
    return int(count)


def check(root: Path, *, target: str, max_behind: int) -> int:
    if max_behind < 0:
        raise ValueError("max_behind must be non-negative")
    recorded = recorded_sha((root / "CURRENT_STATE.md").read_text(encoding="utf-8"))
    # Resolve both names first so missing/shallow history fails closed.
    recorded_full = _git(root, "rev-parse", "--verify", f"{recorded}^{{commit}}")
    target_full = _git(root, "rev-parse", "--verify", f"{target}^{{commit}}")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", recorded_full, target_full],
        cwd=root, capture_output=True, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError(
            "CURRENT_STATE.md reconciliation commit is not an ancestor of the target tree"
        )
    distance = first_parent_distance(root, recorded_full, target_full)
    if distance > max_behind:
        raise RuntimeError(
            f"CURRENT_STATE.md is {distance} first-parent commits behind target "
            f"{target_full[:12]}; allowed drift is {max_behind}. Reconcile the document."
        )
    print(
        f"CURRENT_STATE.md reconciliation {recorded_full[:12]} is "
        f"{distance} first-parent commit(s) behind {target_full[:12]}."
    )
    return distance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--target", required=True)
    parser.add_argument("--max-behind", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        check(args.repository.resolve(), target=args.target, max_behind=args.max_behind)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
