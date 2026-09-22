#!/usr/bin/env python3
"""Fail-closed runtime integrity check for public Local Code Agent entrypoints.

This script intentionally uses only the Python standard library.  It must be able to
explain a broken checkout environment before importing product code, starting OVMS, or
preparing a model.
"""
from __future__ import annotations

import argparse
from importlib import import_module, metadata
from pathlib import Path
import re
import sys
import tomllib


MIN_PYTHON = (3, 11)
# These imports exercise the exact high-value surfaces that have previously failed only
# after the public launcher had already begun useful-looking work.
CRITICAL_IMPORTS = (
    "local_agent.session.event_contract",
    "local_agent.tools.tool_primitives",
    "textual",
    "openai",
    "psutil",
)
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared_runtime_distributions(pyproject: Path) -> tuple[str, ...]:
    """Return direct runtime distribution names declared by this checkout."""
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    dependencies = data.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("pyproject.toml does not declare project.dependencies")

    names: list[str] = []
    for requirement in dependencies:
        if not isinstance(requirement, str):
            raise ValueError("project.dependencies contains a non-string requirement")
        match = _REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise ValueError(f"cannot parse runtime requirement: {requirement!r}")
        names.append(match.group(1))
    return tuple(names)


def runtime_errors(
    pyproject: Path,
    *,
    critical_imports: tuple[str, ...] = CRITICAL_IMPORTS,
) -> list[str]:
    """Return bounded diagnostics; never repair or access the network."""
    errors: list[str] = []
    if sys.version_info < MIN_PYTHON:
        errors.append(
            f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; "
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required"
        )

    try:
        distributions = declared_runtime_distributions(pyproject)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [f"cannot read runtime contract {pyproject}: {exc}"]

    for name in distributions:
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            errors.append(f"declared runtime dependency is not installed: {name}")

    for module_name in critical_imports:
        try:
            import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary must catch broken imports
            errors.append(
                f"runtime import failed: {module_name}: {type(exc).__name__}: {exc}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate this checkout's Python runtime without repairing it")
    parser.add_argument("--pyproject", type=Path, required=True)
    args = parser.parse_args(argv)

    errors = runtime_errors(args.pyproject.resolve())
    if errors:
        print("Local Code Agent runtime preflight failed.", file=sys.stderr)
        print(f"Interpreter: {Path(sys.executable).resolve()}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print("Run .\\install.ps1 to update the managed checkout environment.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
