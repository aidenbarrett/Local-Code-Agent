from __future__ import annotations

from pathlib import Path

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


def test_git_show_still_accepts_normal_revision(loaded):
    _sandbox, _repo, registry, _store, _skills = loaded

    result = registry.get("git_show").handler(rev="HEAD", stat_only=True)

    assert result.ok is True
    assert "commit" in result.summary
