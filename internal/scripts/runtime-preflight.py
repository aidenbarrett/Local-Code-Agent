#!/usr/bin/env python3
"""Fail-closed runtime integrity check for public Local Code Agent entrypoints.

This script intentionally uses only the Python standard library. It must be able to
explain a broken checkout environment before importing product code, starting OVMS, or
preparing a model.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import import_module, metadata
from pathlib import Path
import re
import sys
import tomllib


MIN_PYTHON = (3, 11)
CRITICAL_IMPORTS = (
    "local_agent.session.event_contract",
    "local_agent.tools.tool_primitives",
    "textual",
    "openai",
    "psutil",
)
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<spec>(?:[<>=!~]=?[^;\s,]+(?:\s*,\s*[<>=!~]=?[^;\s,]+)*)?)"
)
_SPECIFIER = re.compile(r"^(~=|==|!=|<=|>=|<|>)(.+)$")


@dataclass(frozen=True)
class RuntimeRequirement:
    name: str
    specifiers: tuple[tuple[str, str], ...]


def _release(value: str) -> tuple[int, ...]:
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", value)
    if match is None:
        raise ValueError(f"unsupported installed version format: {value!r}")
    return tuple(int(piece) for piece in match.group(1).split("."))


def _cmp(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    lhs = left + (0,) * (width - len(left))
    rhs = right + (0,) * (width - len(right))
    return (lhs > rhs) - (lhs < rhs)


def _satisfies(installed: str, specifiers: tuple[tuple[str, str], ...]) -> bool:
    current = _release(installed)
    for operator, raw_target in specifiers:
        target = _release(raw_target)
        comparison = _cmp(current, target)
        if operator == ">=" and comparison < 0:
            return False
        if operator == ">" and comparison <= 0:
            return False
        if operator == "<=" and comparison > 0:
            return False
        if operator == "<" and comparison >= 0:
            return False
        if operator == "==" and comparison != 0:
            return False
        if operator == "!=" and comparison == 0:
            return False
        if operator == "~=":
            if comparison < 0:
                return False
            if len(target) <= 1:
                upper = (target[0] + 1,)
            else:
                upper = target[:-1] + (target[-2] + 1,) if len(target) > 2 else (target[0] + 1,)
            if _cmp(current, upper) >= 0:
                return False
    return True


def declared_runtime_requirements(pyproject: Path) -> tuple[RuntimeRequirement, ...]:
    """Return direct runtime distributions and their declared version constraints."""
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    dependencies = data.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("pyproject.toml does not declare project.dependencies")

    requirements: list[RuntimeRequirement] = []
    for requirement in dependencies:
        if not isinstance(requirement, str):
            raise ValueError("project.dependencies contains a non-string requirement")
        match = _REQUIREMENT.match(requirement)
        if match is None:
            raise ValueError(f"cannot parse runtime requirement: {requirement!r}")
        parsed: list[tuple[str, str]] = []
        spec = match.group("spec").strip()
        if spec:
            for item in spec.split(","):
                token = item.strip()
                spec_match = _SPECIFIER.fullmatch(token)
                if spec_match is None:
                    raise ValueError(f"unsupported runtime version specifier: {token!r}")
                parsed.append((spec_match.group(1), spec_match.group(2)))
        requirements.append(RuntimeRequirement(match.group("name"), tuple(parsed)))
    return tuple(requirements)


def declared_runtime_distributions(pyproject: Path) -> tuple[str, ...]:
    """Compatibility view of the declared direct runtime distribution names."""
    return tuple(requirement.name for requirement in declared_runtime_requirements(pyproject))


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
        requirements = declared_runtime_requirements(pyproject)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return [f"cannot read runtime contract {pyproject}: {exc}"]

    for requirement in requirements:
        try:
            installed = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            errors.append(f"declared runtime dependency is not installed: {requirement.name}")
            continue
        try:
            compatible = _satisfies(installed, requirement.specifiers)
        except ValueError as exc:
            errors.append(f"cannot validate {requirement.name} {installed}: {exc}")
            continue
        if not compatible:
            rendered = ",".join(op + version for op, version in requirement.specifiers) or "any"
            errors.append(
                f"declared runtime dependency has incompatible version: "
                f"{requirement.name} {installed}; required {rendered}"
            )

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
