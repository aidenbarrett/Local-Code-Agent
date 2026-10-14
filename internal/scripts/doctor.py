#!/usr/bin/env python3
"""`.\\local-code-agent.ps1 doctor`: is this machine and repository ready, and what next?

Read-only by contract: it never installs, downloads, writes or starts anything. It
composes the owners that already decide each fact (``resolve_preset``,
``model_weights.weight_state``, ``serve.make_plan``/``serve.status``,
``load_repo_config``, ``runtime-preflight.runtime_errors``) instead of judging them a
second time. Every row keeps what is configured apart from what was observed, and
every missing prerequisite names exactly one supported root-level next action.

On Windows, two facts belong to the launcher's own PowerShell owners: the MSVC
installation (``scripts/msvc.ps1``) and the managed OVMS executable
(``Set-ManagedOvmsEnvironment``). The launcher passes them in; run without it, those
rows stay unknown rather than being guessed.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tomllib
from types import ModuleType
from typing import Any

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent.config import MODEL_PRESETS, ConfigError, find_repo_root, load_repo_config  # noqa: E402
from local_agent.repo_setup import init_command  # noqa: E402
from local_agent.public_commands import powershell_literal, public_entrypoint  # noqa: E402
from serving import serve  # noqa: E402
from serving.model_choice import ModelChoiceError, resolve_preset  # noqa: E402
from serving.model_store import default_runtime_root  # noqa: E402

READY = "READY"
MISSING = "MISSING"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"
INFO = "INFO"
_NOT_READY = frozenset({MISSING, BLOCKED})
EXIT_READY = 0
EXIT_NOT_READY = 3
_INSTALL = public_entrypoint("install.ps1")
_VIA_LAUNCHER = f"run it through {public_entrypoint()} doctor"
_POSIX_COMPILERS = ("c++", "g++", "clang++")


@dataclass(frozen=True)
class Check:
    """One readiness row. ``next_action`` is set exactly when the row is not ready."""

    name: str
    state: str
    configured: str | None = None
    observed: str | None = None
    next_action: str | None = None

    def __post_init__(self) -> None:
        if (self.state in _NOT_READY) != (self.next_action is not None):
            raise ValueError(f"{self.name}: a next action is required exactly when not ready")


@dataclass(frozen=True)
class LauncherFacts:
    """Facts only the PowerShell launcher can observe. ``None`` means not supplied."""

    probed: bool = False
    msvc_installation: str | None = None


Which = Callable[[str], str | None]
EndpointStatus = Callable[[dict[str, Any]], dict[str, Any]]

# serving.serve is untyped; these are its two read-only entry points doctor uses.
_make_plan: Callable[..., dict[str, Any]] = serve.make_plan
_pull_command: Callable[[str], str] = serve.pull_command


@dataclass(frozen=True)
class Observers:
    """Where each observation comes from; tests substitute them."""

    env: Mapping[str, str]
    which: Which
    endpoint_status: EndpointStatus
    windows: bool
    launcher: LauncherFacts

    @classmethod
    def of_this_process(cls, launcher: LauncherFacts) -> Observers:
        return cls(os.environ, shutil.which, serve.status, os.name == "nt", launcher)


def _script(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, INTERNAL / "scripts" / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def check_repository(start: Path) -> Check:
    root = find_repo_root(start)
    try:
        repo = load_repo_config(root)
    except (ConfigError, tomllib.TOMLDecodeError, OSError) as exc:
        if not (root / ".local-agent.toml").is_file():
            return Check(
                "Repository", MISSING, configured=f"none at {root}",
                observed="Local Code Agent will not guess how to build an undeclared repository",
                next_action=init_command(root),
            )
        return Check("Repository", BLOCKED, configured=str(root / ".local-agent.toml"),
                     observed=f"invalid: {exc}",
                     next_action=f"correct {root / '.local-agent.toml'}, then rerun doctor")
    policy = repo.policy
    permissions = (("build", policy.allow_build), ("test", policy.allow_test),
                   ("patch", policy.allow_patch), ("commit", policy.allow_commit))
    allowed = [name for name, value in permissions if value]
    return Check(
        "Repository", READY,
        configured=(f"{repo.name} at {repo.root}; profiles {', '.join(sorted(repo.profiles))} "
                    f"(default {repo.default_profile}); policy allows "
                    f"{', '.join(allowed) if allowed else 'no build, test, patch or commit'}"),
    )


def check_python(pyproject: Path) -> Check:
    preflight = _script("runtime_preflight_for_doctor", "runtime-preflight.py")
    errors = preflight.runtime_errors(pyproject)
    version = ".".join(str(part) for part in sys.version_info[:3])
    if errors:
        return Check("Python", BLOCKED, observed=f"{version} at {sys.executable}: {errors[0]}",
                     next_action=_INSTALL)
    return Check("Python", READY, observed=f"{version}; runtime contract satisfied")


def check_artifact_ignores(start: Path, which: Which) -> Check:
    """Observe declared output directories through the existing Git process owner."""
    from local_agent.tools.git import observe_output_ignores
    from local_agent.tools.tool_primitives import ToolError

    try:
        repo = load_repo_config(find_repo_root(start))
    except (ConfigError, tomllib.TOMLDecodeError, OSError):
        return Check("Build artifacts", UNKNOWN, observed="requires a valid repository declaration")
    if which("git") is None:
        return Check("Build artifacts", UNKNOWN, observed="requires Git")
    paths = tuple(dict.fromkeys((repo.build_dir, repo.run_dir)))
    configured = ", ".join(repr(path) for path in paths)
    try:
        facts = observe_output_ignores(repo.root, paths)
    except ToolError as exc:
        return Check("Build artifacts", UNKNOWN, configured=configured, observed=str(exc))
    return _artifact_ignore_check(repo.root, configured, facts.missing, facts.tracked)


def _artifact_ignore_check(
    root: Path, configured: str, missing: tuple[str, ...], tracked: tuple[str, ...],
) -> Check:
    if tracked:
        return Check("Build artifacts", BLOCKED, configured=configured,
                     observed=f"{tracked[0]!r} contains tracked files",
                     next_action="choose untracked output directories in .local-agent.toml "
                                 "and its profile commands, then rerun doctor")
    if not missing:
        return Check("Build artifacts", READY, configured=configured,
                     observed="Git ignores both build outputs and run logs")
    if any("\n" in path or "\r" in path for path in missing):
        return Check("Build artifacts", BLOCKED, configured=configured,
                     observed="an output directory contains a line break",
                     next_action="choose output paths without line breaks in .local-agent.toml "
                                 "and its profile commands, then rerun doctor")
    ignore_file = root / ".gitignore"
    if ignore_file.is_symlink():
        return Check("Build artifacts", BLOCKED, configured=configured,
                     observed=".gitignore is a symlink; the suggested edit would follow it",
                     next_action="review the symlink and use a regular repository .gitignore, "
                                 "then rerun doctor")
    patterns = ["/" + "".join("\\" + char if char in "\\*?[]" else char for char in path)
                + "/" for path in missing]
    values = ", ".join(powershell_literal(value) for value in ("", *patterns))
    return Check("Build artifacts", MISSING, configured=configured,
                 observed="unignored outputs would change repository status during build/test",
                 next_action=f"Add-Content -LiteralPath {powershell_literal(ignore_file)} "
                             f"-Encoding utf8 -Value @({values})")


def check_on_path(name: str, command: str, which: Which) -> Check:
    found = which(command)
    if found is None:
        return Check(name, MISSING, observed=f"{command} is not on PATH", next_action=_INSTALL)
    return Check(name, READY, observed=f"on PATH: {found}")


def check_compiler(observers: Observers) -> Check:
    launcher = observers.launcher
    if observers.windows:
        if not launcher.probed:
            return Check("C++ compiler", UNKNOWN, observed=f"MSVC not probed; {_VIA_LAUNCHER}")
        if not launcher.msvc_installation:
            return Check("C++ compiler", MISSING,
                         observed="no Visual Studio installation with the C++ tools",
                         next_action=_INSTALL)
        return Check("C++ compiler", READY, observed=f"MSVC at {launcher.msvc_installation}")
    for command in _POSIX_COMPILERS:
        found = observers.which(command)
        if found is not None:
            return Check("C++ compiler", READY, observed=f"on PATH: {found}")
    return Check("C++ compiler", MISSING, observed=f"none of {', '.join(_POSIX_COMPILERS)} on PATH",
                 next_action=_INSTALL)


def check_profile(runtime_root: Path, explicit: str | None) -> tuple[Check, str | None]:
    try:
        resolved = resolve_preset(explicit, runtime_root)
    except ModelChoiceError as exc:
        return Check("Model profile", BLOCKED, observed=str(exc),
                     next_action=f"{public_entrypoint()} models use <profile>  "
                                 f"({public_entrypoint()} models lists them)"), None
    config = MODEL_PRESETS[resolved.name]
    how = {"explicit": "--profile", "stored": "your models use choice",
           "default": "the default"}[resolved.source]
    return Check("Model profile", READY,
                 configured=f"{resolved.name} ({how}): {config.model} on {config.device} "
                            f"via {config.runtime}"), resolved.name


def _plan(profile: str, runtime_root: Path, env: Mapping[str, str]) -> dict[str, Any]:
    config = MODEL_PRESETS[profile]
    executable = env.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    return _make_plan(profile, config, runtime_root, executable=executable)


def check_model_server(profile: str, runtime_root: Path, observers: Observers) -> Check:
    config = MODEL_PRESETS[profile]
    env = observers.env
    if config.runtime == "ovms" and observers.windows:
        if not observers.launcher.probed:
            return Check("Model server", UNKNOWN, observed=f"OVMS not located; {_VIA_LAUNCHER}")
        executable = env.get("LCA_OVMS_EXECUTABLE")
        if not executable:
            return Check("Model server", MISSING,
                         observed=f"OVMS is not installed under {runtime_root / 'tools'}",
                         next_action=_INSTALL)
        return Check("Model server", READY, observed=f"OVMS at {executable}")
    command = str(_plan(profile, runtime_root, env)["exe"])
    found = observers.which(command)
    if found is None:
        action = (_INSTALL if config.runtime == "ovms"
                  else f"put {command} on PATH (llama.cpp profiles are set up by hand)")
        return Check("Model server", MISSING, observed=f"{command} is not on PATH",
                     next_action=action)
    return Check("Model server", READY, observed=f"on PATH: {found}")


def check_weights(profile: str, runtime_root: Path, env: Mapping[str, str]) -> Check:
    weights = _script("model_weights_for_doctor", "model_weights.py")
    folder = _plan(profile, runtime_root, env)["model_dir"]
    state = weights.weight_state(profile, runtime_root)
    if state == "downloaded":
        return Check("Weights", READY, observed=f"complete in {folder}")
    if MODEL_PRESETS[profile].runtime == "llamacpp":
        return Check("Weights", MISSING, observed=f"{state}: {folder}",
                     next_action=f"copy the GGUF file to {folder}")
    return Check("Weights", MISSING, observed=f"{state} in {folder}",
                 next_action=_pull_command(profile))


def check_endpoint(profile: str, runtime_root: Path, env: Mapping[str, str],
                   status: EndpointStatus) -> Check:
    config = MODEL_PRESETS[profile]
    plan = _plan(profile, runtime_root, env)
    configured = f"http://{plan['host']}:{plan['port']} serving {config.model}"
    try:
        observed = status(plan)
    except serve.Refusal as exc:
        return Check("Endpoint", UNKNOWN, configured=configured, observed=str(exc))
    if observed.get("healthy"):
        return Check("Endpoint", READY, configured=configured,
                     observed=f"running, owned by Local Code Agent (pid {observed.get('pid')})")
    if observed.get("process_alive"):
        return Check("Endpoint", INFO, configured=configured,
                     observed="owned server is running but not ready; "
                              "opening a session restarts it")
    answered = (observed.get("models_http_status"), observed.get("health_http_status"))
    if any(code is not None for code in answered):
        return Check("Endpoint", BLOCKED, configured=configured,
                     observed="another server answers on this port; "
                              "a session will refuse to use it",
                     next_action=f"stop the server listening on {plan['host']}:{plan['port']}, "
                                 "then rerun doctor")
    return Check("Endpoint", INFO, configured=configured,
                 observed=f"not running; {public_entrypoint()} starts it when a session opens")


def check_execution(repo: Path, profile: str | None) -> Check:
    profile_arg = "" if profile is None else f" --profile {powershell_literal(profile)}"
    return Check("Execution", INFO,
                 configured="build and test run only in a session opened with --allow-execution",
                 observed=f"to allow them: {public_entrypoint()} session "
                          f"--repo {powershell_literal(repo)}{profile_arg} --allow-execution")


def assess(
    start: Path,
    runtime_root: Path,
    *,
    pyproject: Path,
    explicit_profile: str | None = None,
    observers: Observers | None = None,
) -> list[Check]:
    """Every readiness row, in the order a user should fix them."""
    seen = observers or Observers.of_this_process(LauncherFacts())
    python_check = check_python(pyproject)
    artifacts = (check_artifact_ignores(start, seen.which) if python_check.state == READY
                 else Check("Build artifacts", UNKNOWN,
                            observed="requires a usable Python runtime; fix the Python row first"))
    checks = [
        check_repository(start),
        python_check,
        check_on_path("Git", "git", seen.which),
        artifacts,
        check_on_path("CMake", "cmake", seen.which),
        check_compiler(seen),
    ]
    profile_check, profile = check_profile(runtime_root, explicit_profile)
    checks.append(profile_check)
    if profile is None:
        dependent = "depends on a usable model profile"
        checks += [Check(name, UNKNOWN, observed=dependent)
                   for name in ("Model server", "Weights", "Endpoint")]
    else:
        try:
            checks += [
                check_model_server(profile, runtime_root, seen),
                check_weights(profile, runtime_root, seen.env),
                check_endpoint(profile, runtime_root, seen.env, seen.endpoint_status),
            ]
        except serve.Refusal as exc:
            checks.append(Check("Model profile plan", BLOCKED, observed=str(exc),
                                next_action=f"{public_entrypoint()} models use <profile>"))
    checks.append(check_execution(start.resolve(), profile))
    return checks


def render(checks: list[Check], *, repo: Path, profile: str | None = None) -> str:
    profile_arg = "" if profile is None else f" --profile {powershell_literal(profile)}"
    lines = ["LOCAL CODE AGENT READINESS",
             "Check only: nothing was installed, downloaded, written or started.", ""]
    for check in checks:
        lines.append(f"{check.name:<14} {check.state}")
        if check.configured:
            lines.append(f"{'':<14} configured: {check.configured}")
        if check.observed:
            lines.append(f"{'':<14} observed:   {check.observed}")
        if check.next_action:
            lines.append(f"{'':<14} next:       {check.next_action}")
    blocking = [check for check in checks if check.state in _NOT_READY]
    lines.append("")
    if blocking:
        lines.append(f"Not ready: {len(blocking)} item(s). Do the first next action, "
                     f"then rerun {public_entrypoint()} doctor "
                     f"--repo {powershell_literal(repo)}{profile_arg}")
    else:
        lines.append(f"Ready. Open the Session Hub: {public_entrypoint()} "
                     f"session --repo {powershell_literal(repo)}{profile_arg}")
    unknown = [check.name for check in checks if check.state == UNKNOWN]
    if unknown:
        lines.append(f"Not observed: {', '.join(unknown)}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-code-agent doctor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="repository root or a path inside it")
    parser.add_argument("--profile", default=None, help="check this model preset instead of yours")
    parser.add_argument("--runtime-root", type=Path, default=None)
    launcher = parser.add_argument_group("supplied by local-code-agent.ps1")
    launcher.add_argument("--launcher-probed", action="store_true", help=argparse.SUPPRESS)
    launcher.add_argument("--msvc-installation", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    launcher_facts = LauncherFacts(args.launcher_probed, args.msvc_installation or None)
    checks = assess(
        args.repo,
        args.runtime_root or default_runtime_root(),
        pyproject=INTERNAL.parent / "pyproject.toml",
        explicit_profile=args.profile,
        observers=Observers.of_this_process(launcher_facts),
    )
    sys.stdout.write(render(checks, repo=args.repo.resolve(), profile=args.profile) + "\n")
    return EXIT_NOT_READY if any(check.state in _NOT_READY for check in checks) else EXIT_READY


if __name__ == "__main__":
    raise SystemExit(main())
