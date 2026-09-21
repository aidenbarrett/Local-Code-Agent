#!/usr/bin/env python3
"""Refuse pull-request diffs that modify frozen experiment evidence."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

FROZEN_PREFIX = "internal/experiments/"
FROZEN_ROOT = "internal/experiments"


def _normalize(path: str) -> str:
    if not isinstance(path, str):
        raise TypeError("changed path must be text")
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value or value.startswith("/"):
        raise ValueError("changed path must be repository-relative")
    if any(part == ".." for part in value.split("/")):
        raise ValueError("changed path cannot traverse outside the repository")
    return value


def frozen_experiment_changes(paths: Iterable[str]) -> tuple[str, ...]:
    """Return the unique frozen-evidence paths touched by a diff."""
    changed = {
        value
        for value in (_normalize(path) for path in paths)
        if value == FROZEN_ROOT or value.startswith(FROZEN_PREFIX)
    }
    return tuple(sorted(changed))


def _read_paths(*, nul: bool) -> list[str]:
    if nul:
        raw = sys.stdin.buffer.read()
        return [part.decode("utf-8") for part in raw.split(b"\0") if part]
    return [line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nul", action="store_true", help="read NUL-delimited paths from stdin")
    args = parser.parse_args(argv)
    try:
        frozen = frozen_experiment_changes(_read_paths(nul=args.nul))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        print(f"FROZEN EXPERIMENT GATE: invalid changed path: {exc}", file=sys.stderr)
        return 3
    if frozen:
        print("FROZEN EXPERIMENT GATE: historical experiment evidence is immutable.", file=sys.stderr)
        for path in frozen:
            print(f"  {path}", file=sys.stderr)
        print(
            "Create a new experiment generation or new artifact path instead of modifying frozen evidence.",
            file=sys.stderr,
        )
        return 2
    print("frozen experiment evidence untouched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
