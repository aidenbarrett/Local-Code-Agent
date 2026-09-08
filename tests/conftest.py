from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SANDBOX = REPO / "benchmark_fixture" / "cpp_project"

sys.path.insert(0, str(REPO))


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
