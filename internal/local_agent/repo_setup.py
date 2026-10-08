"""Declare a repository for Local Code Agent: inspect it, propose, and write only on request.

``load_repo_config`` stays the one authority on what a declaration means. This module
only proposes a declaration in that existing schema and argv form, and writes it when
the user asks. It never runs a discovered command, reads no README or model output,
and never enables execution: build and test still run only in a session opened with
``--allow-execution``.

A proposal is offered only when the repository root has exactly one recognised build
system and it is CMake, the system the product's build and test verification is
qualified on. Anything else is refused before anything is written, naming what was
found.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import tomllib

from .config import DEFAULT_CONFIG_NAME, ConfigError, RepoConfig, find_repo_root, load_repo_config

# Root-level markers of a build system, by file name.
_MARKERS: dict[str, str] = {
    "CMakeLists.txt": "CMake",
    "meson.build": "Meson",
    "Cargo.toml": "Cargo",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "pom.xml": "Maven",
    "package.json": "npm",
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "Makefile": "Make",
    "GNUmakefile": "Make",
    "configure.ac": "Autotools",
    "BUILD.bazel": "Bazel",
    "WORKSPACE": "Bazel",
    "MODULE.bazel": "Bazel",
}
_SOLUTION_SUFFIX = ".sln"
PROPOSABLE = "CMake"

# The same profile the product's own C++ fixture and cxxopts qualification declare.
_CMAKE_PROFILE = (
    '[profiles.debug]\n'
    'configure = ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]\n'
    'build = ["cmake", "--build", "build", "--config", "Debug", "--parallel", "4"]\n'
    'test = ["ctest", "--test-dir", "build", "-C", "Debug", "--output-on-failure"]\n'
)


def init_command(root: Path) -> str:
    """The one supported next action for a repository with no declaration."""
    return f'.\\local-code-agent.ps1 init --repo "{root}"'


class RepoSetupError(Exception):
    """A declaration cannot be proposed or written; the message says why."""


@dataclass(frozen=True)
class RepoInspection:
    """What ``init`` found. Exactly one of ``declared``, ``invalid``, ``proposal`` or
    ``refusal`` describes the outcome."""

    root: Path
    opened_from: Path
    declared: RepoConfig | None = None
    invalid: str | None = None
    build_systems: tuple[str, ...] = ()
    proposal: str | None = None
    refusal: str | None = None

    @property
    def config_path(self) -> Path:
        return self.root / DEFAULT_CONFIG_NAME


def build_systems(root: Path) -> tuple[str, ...]:
    """Recognised build systems at the root, each named once, in a stable order."""
    found: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if not entry.is_file():
            continue
        system = _MARKERS.get(entry.name)
        if system is None and entry.name.endswith(_SOLUTION_SUFFIX):
            system = "MSBuild"
        if system is not None and system not in found:
            found.append(system)
    return tuple(found)


def toml_basic_string(value: str) -> str:
    """``value`` as a TOML basic string.

    TOML accepts any Unicode scalar value literally except the quote, the backslash
    and control characters, which are escaped. JSON escaping is not a substitute: it
    writes non-BMP characters as surrogate pairs, which TOML rejects (#469). A lone
    surrogate (an undecodable file name) has no TOML form and is refused.
    """
    out = ['"']
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise RepoSetupError(f"{value!r} contains a character TOML cannot represent")
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def propose_cmake(name: str) -> str:
    """A reviewed CMake/CTest declaration in the existing schema; nothing is executed."""
    return (
        f"# Proposed by `.\\local-code-agent.ps1 init` because the root has CMakeLists.txt.\n"
        "# Review it: these commands run only in a session opened with --allow-execution.\n"
        "[repo]\n"
        f"name = {toml_basic_string(name)}\n"
        'build_dir = "build"\n'
        'run_dir = ".local-agent/runs"\n'
        'default_profile = "debug"\n'
        "\n"
        f"{_CMAKE_PROFILE}"
        "\n"
        "[policy]\n"
        "allow_build = true\n"
        "allow_test = true\n"
        "allow_patch = false\n"
        "allow_commit = false\n"
    )


def validate_declaration(text: str) -> RepoConfig:
    """Parse ``text`` with the one configuration authority before it is ever written."""
    with tempfile.TemporaryDirectory(prefix="lca-init-") as scratch:
        Path(scratch, DEFAULT_CONFIG_NAME).write_text(text, encoding="utf-8")
        try:
            return load_repo_config(Path(scratch))
        except (ConfigError, tomllib.TOMLDecodeError) as exc:
            raise RepoSetupError(f"the proposed declaration is invalid: {exc}") from exc


def inspect_repository(start: Path) -> RepoInspection:
    opened = Path(start)
    if not opened.is_dir():
        raise RepoSetupError(f"{opened} is not a directory")
    root = find_repo_root(opened)
    path = root / DEFAULT_CONFIG_NAME
    if path.is_file():
        try:
            return RepoInspection(root, opened.resolve(), declared=load_repo_config(root))
        except (ConfigError, tomllib.TOMLDecodeError, OSError) as exc:
            return RepoInspection(root, opened.resolve(), invalid=str(exc))
    systems = build_systems(root)
    if systems == (PROPOSABLE,):
        proposal = propose_cmake(root.name)
        validate_declaration(proposal)
        return RepoInspection(root, opened.resolve(), build_systems=systems, proposal=proposal)
    if not systems:
        refusal = f"no recognised build system at {root}"
    elif PROPOSABLE in systems:
        refusal = (f"more than one build system at {root} ({', '.join(systems)}); "
                   "init will not guess which one to verify")
    else:
        refusal = (f"{', '.join(systems)} at {root}: init proposes CMake/CTest "
                   "declarations only")
    return RepoInspection(root, opened.resolve(), build_systems=systems, refusal=refusal)


def write_proposal(inspection: RepoInspection) -> Path:
    """Write the reviewed proposal; never replaces an existing declaration."""
    if inspection.proposal is None:
        raise RepoSetupError("there is no proposal to write")
    validate_declaration(inspection.proposal)
    path = inspection.config_path
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise RepoSetupError(f"{path} already exists; init never replaces a declaration") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(inspection.proposal)
    load_repo_config(inspection.root)
    return path


__all__ = [
    "PROPOSABLE",
    "RepoInspection",
    "RepoSetupError",
    "build_systems",
    "init_command",
    "inspect_repository",
    "propose_cmake",
    "toml_basic_string",
    "validate_declaration",
    "write_proposal",
]
