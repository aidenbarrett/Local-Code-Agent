from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

import pytest

from local_agent.tools.tool_primitives import ToolError


def test_git_show_refuses_option_injection_without_writing_outside_repo(loaded, tmp_path: Path):
    _sandbox, _repo, registry, _store, _skills = loaded
    outside = tmp_path / "git-show-escaped.txt"

    with pytest.raises(ToolError, match="revision-like values may not be options"):
        registry.get("git_show").handler(
            rev=f"--output={outside}",
            stat_only=False,
        )

    assert not outside.exists()


@pytest.mark.parametrize("rev", ["--ext-diff", "-p", "--output=anything"])
def test_git_show_refuses_every_leading_option(loaded, rev: str):
    _sandbox, _repo, registry, _store, _skills = loaded

    with pytest.raises(ToolError, match="revision-like values may not be options"):
        registry.get("git_show").handler(rev=rev, stat_only=True)


@pytest.mark.parametrize("base", ["--all", "--octopus", "-h"])
def test_git_branch_info_refuses_option_shaped_base(loaded, base: str):
    _sandbox, _repo, registry, _store, _skills = loaded

    with pytest.raises(ToolError, match="revision-like values may not be options"):
        registry.get("git_branch_info").handler(base=base)


def test_git_status_does_not_execute_repo_configured_fsmonitor(loaded, tmp_path: Path):
    sandbox, _repo, registry, _store, _skills = loaded
    marker = tmp_path / "fsmonitor-executed.txt"

    if os.name == "nt":
        hook = tmp_path / "fsmonitor.cmd"
        hook.write_text(
            "@echo off\r\n"
            f">\"{marker}\" echo executed\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        hook = tmp_path / "fsmonitor.sh"
        hook.write_text(
            "#!/bin/sh\n"
            f"printf executed > {shlex.quote(str(marker))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

    subprocess.run(
        ["git", "config", "core.fsmonitor", str(hook)],
        cwd=sandbox.root,
        check=True,
    )

    result = registry.get("git_status").handler()

    assert result.ok is True
    assert not marker.exists(), "Risk.READ git_status executed repository-configured code"


def test_git_show_still_accepts_normal_revision(loaded):
    _sandbox, _repo, registry, _store, _skills = loaded

    result = registry.get("git_show").handler(rev="HEAD", stat_only=True)

    assert result.ok is True
    assert "commit" in result.summary
