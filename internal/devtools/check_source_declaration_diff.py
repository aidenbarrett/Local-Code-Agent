#!/usr/bin/env python3
"""Require an instrument declaration in any PR that changes hashed source."""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / "internal" / "local_agent" / "provenance.py"
DECLARATION = "internal/INSTRUMENT.json"


def _normalize(path: str) -> str:
    value = path.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value or value.startswith("/") or any(part == ".." for part in value.split("/")):
        raise ValueError(f"invalid repository-relative path: {path!r}")
    return value


def _load_hashed_surface(path: Path = PROVENANCE) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot read provenance surface: {exc}") from exc
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in {"_HASHED", "_HASHED_FILES"}:
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(f"cannot parse {target.id} from provenance.py") from exc
    hashed = values.get("_HASHED")
    hashed_files = values.get("_HASHED_FILES")
    if not isinstance(hashed, tuple) or not isinstance(hashed_files, tuple):
        raise ValueError("provenance.py does not expose the expected hashed-surface declarations")
    return hashed, hashed_files


def hashed_source_changes(
    paths: Iterable[str], *, provenance_path: Path = PROVENANCE
) -> tuple[str, ...]:
    hashed_dirs, hashed_files = _load_hashed_surface(provenance_path)
    out: set[str] = set()
    for raw in paths:
        value = _normalize(raw)
        if value in hashed_files:
            out.add(value)
            continue
        for folder, pattern in hashed_dirs:
            prefix = folder.rstrip("/") + "/"
            if not value.startswith(prefix):
                continue
            relative = PurePosixPath(value[len(prefix):])
            if relative.match(pattern):
                out.add(value)
                break
    return tuple(sorted(out))


def declaration_error(
    paths: Iterable[str], *, provenance_path: Path = PROVENANCE
) -> tuple[str, tuple[str, ...]] | None:
    normalized = tuple(_normalize(path) for path in paths)
    hashed = hashed_source_changes(normalized, provenance_path=provenance_path)
    if hashed and DECLARATION not in normalized:
        return (
            "hashed source changed without internal/INSTRUMENT.json in the same pull request",
            hashed,
        )
    return None


def _read_paths(*, nul: bool) -> list[str]:
    if nul:
        return [part.decode("utf-8") for part in sys.stdin.buffer.read().split(b"\0") if part]
    return [line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nul", action="store_true", help="read NUL-delimited paths from stdin")
    args = parser.parse_args(argv)
    try:
        paths = _read_paths(nul=args.nul)
        error = declaration_error(paths)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        print(f"SOURCE DECLARATION GATE: {exc}", file=sys.stderr)
        return 3
    if error is not None:
        message, hashed = error
        print(f"SOURCE DECLARATION GATE: {message}", file=sys.stderr)
        for path in hashed:
            print(f"  {path}", file=sys.stderr)
        print(
            "Run `python internal/devtools/finalize_change.py --write-source` and commit the declaration before pushing.",
            file=sys.stderr,
        )
        return 2
    print("source declaration presence is coherent with this diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
