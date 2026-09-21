#!/usr/bin/env python3
"""Keep test imports compatible with the offline runner's import root."""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_ROOT = ROOT / "internal" / "tests"


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    line: int
    module: str


def _forbidden_module(name: str | None) -> bool:
    return bool(name) and (name == "internal" or name.startswith("internal."))


def violations_in_file(path: Path) -> tuple[ImportViolation, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    out: list[ImportViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_module(alias.name):
                    out.append(ImportViolation(path, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and _forbidden_module(node.module):
            out.append(ImportViolation(path, node.lineno, node.module or "internal"))
    return tuple(out)


def find_violations(root: Path = DEFAULT_TEST_ROOT) -> tuple[ImportViolation, ...]:
    if not root.is_dir():
        raise ValueError(f"test root does not exist: {root}")
    out: list[ImportViolation] = []
    for path in sorted(root.rglob("test_*.py")):
        out.extend(violations_in_file(path))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_TEST_ROOT)
    args = parser.parse_args(argv)
    try:
        violations = find_violations(args.path)
    except ValueError as exc:
        print(f"TEST IMPORT BOUNDARY: {exc}", file=sys.stderr)
        return 3
    if violations:
        print(
            "TEST IMPORT BOUNDARY: tests must import from the `internal/` import root, not `internal.*`.",
            file=sys.stderr,
        )
        print(
            "Real pytest adds the repository root, while the offline compatibility runner adds `internal/`; `internal.*` can therefore pass pytest and fail offline CI.",
            file=sys.stderr,
        )
        for violation in violations:
            try:
                label = violation.path.relative_to(ROOT)
            except ValueError:
                label = violation.path
            print(f"  {label}:{violation.line}: {violation.module}", file=sys.stderr)
        return 2
    print("test import boundaries are compatible with the offline runner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
