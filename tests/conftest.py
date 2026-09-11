from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SANDBOX = REPO / "benchmark_fixture" / "cpp_project"

sys.path.insert(0, str(REPO))


def pytest_configure(config):
    """On Windows, put pytest's temp tree somewhere short and unshared.

    Cases with `precondition="built"` configure and build a real CMake project
    inside `tmp_path`. CMake then nests its own tree underneath, and MSVC's
    generator is the deepest of them: objects, dependency files and .tlog files
    land roughly a hundred characters below the worktree root. Add pytest's
    default basetemp, about sixty characters on Windows before the per-test
    directory, and the whole thing crosses MAX_PATH.

    Measured on the work laptop, the same `run_case` call twice:

        worktree path 143 chars   configure (debug) FAILED in 4.9s
        worktree path  55 chars   built ok, 4 tests registered

    It surfaced as `PreconditionError: configure failed`, which says nothing
    about paths.

    Short AND unique. A fixed `<temp>/lca` would be short enough, and two
    concurrent runs would then share one basetemp, which pytest empties on
    startup: the second run would delete the first run's worktrees underneath
    it. `mkdtemp` gives each invocation its own directory atomically, and they
    all sit under one parent so `<temp>/lca` is the only thing anybody has to
    delete to clean up.

    POSIX is left exactly as it was: no path limit, no reason to move anything,
    and Linux results have to stay comparable. `measurement/run_test_suite.py`
    needs none of this either, because it already hands out `mkdtemp` paths.
    This is a pytest-on-Windows problem and nothing else.

    An explicit --basetemp always wins; somebody who asked for a location gets
    it, including its length.
    """
    if sys.platform != "win32":
        return
    if getattr(config.option, "basetemp", None):
        return
    parent = Path(tempfile.gettempdir()) / "lca"
    parent.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = tempfile.mkdtemp(prefix="", dir=str(parent))


def _apply(root: Path, scenario: str) -> None:
    subprocess.run(
        [sys.executable, str(root / "scripts" / "apply_scenario.py"), scenario],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def sandbox(tmp_path: Path):
    """A private copy of the C++ torture repo, with a scenario switch."""
    dest = tmp_path / "cpp_project"
    shutil.copytree(
        SANDBOX,
        dest,
        ignore=shutil.ignore_patterns("build", ".local-agent"),
    )
    subprocess.run(["git", "init", "-q"], cwd=str(dest), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dest), check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "baseline"],
        cwd=str(dest),
        check=True,
    )

    class Sandbox:
        root = dest

        @staticmethod
        def scenario(name: str) -> None:
            _apply(dest, name)

    return Sandbox()


@pytest.fixture
def loaded(sandbox):
    """Sandbox plus a built registry and skill library."""
    from local_agent.agent import SkillLibrary
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    # Skills live in the agent repo, not in the sandbox under test.
    repo = load_repo_config(sandbox.root)
    registry, ctx, store = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    return sandbox, repo, registry, store, skills
