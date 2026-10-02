"""git_stage and git_commit never execute the repository's hooks.

A hook is code from the repository under work. The git_commit tool ran plain
``git commit -m``, so an approved commit executed pre-commit, commit-msg and
post-commit hooks, while the Session Hub's commit path pinned them off.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry

HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")


def _git(root, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _plant(directory, marker_dir) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in HOOKS:
        hook = directory / name
        hook.write_text(f"#!/bin/sh\necho ran > '{(marker_dir / name).as_posix()}'\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.parametrize("location", ["default", "configured"])
def test_an_approved_commit_runs_no_repository_hook(sandbox, tmp_path, location):
    markers = tmp_path / "markers"
    markers.mkdir()
    if location == "default":
        _plant(sandbox.root / ".git" / "hooks", markers)
    else:
        _plant(sandbox.root / "repo-hooks", markers)
        _git(sandbox.root, "config", "core.hooksPath", "repo-hooks")
    _git(sandbox.root, "config", "user.name", "t")
    _git(sandbox.root, "config", "user.email", "t@e.invalid")
    before = _git(sandbox.root, "rev-parse", "HEAD").strip()
    (sandbox.root / "README.md").write_text("changed\n", encoding="utf-8")

    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    registry.get("git_stage").handler(paths=["README.md"])
    result = registry.get("git_commit").handler(message="update the readme")

    assert result.ok
    assert _git(sandbox.root, "rev-parse", "HEAD~1").strip() == before
    assert sorted(path.name for path in markers.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="the control proves the planted hook is executable by git")
def test_the_planted_hook_does_run_under_plain_git(sandbox, tmp_path):
    """Control: the same hook fires for an ordinary commit, so the test above is not vacuous."""
    markers = tmp_path / "markers"
    markers.mkdir()
    _plant(sandbox.root / ".git" / "hooks", markers)
    (sandbox.root / "README.md").write_text("changed\n", encoding="utf-8")
    _git(sandbox.root, "add", "README.md")
    _git(sandbox.root, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "plain")
    assert (markers / "pre-commit").exists() and (markers / "post-commit").exists()
