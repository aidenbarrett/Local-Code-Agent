"""The read-only Git tools never run a repository's filter programs (#464, #398).

``git status``, a working-tree ``git diff`` and ``git add`` run a configured clean
filter, which is a program named in Git configuration. The controller already refuses
candidate work in such a repository; the tools that the git-review worker and the
repository-state route use ran the filter anyway. Both layers now share one detector
and refuse. Reads that never touch the working tree (staged diff, log) stay allowed.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.tool_primitives import ToolError


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],  # noqa: S607
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout


def _install_filter(root: Path, marker: Path, *, pattern: str = "*.txt") -> None:
    """A clean/smudge filter that leaves a marker whenever git runs it."""
    script = root.parent / "filter.py"
    script.write_text(
        "import sys\n"
        f"open({str(marker)!r}, 'a').write('ran\\n')\n"
        "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    _git(root, "config", "filter.audit.clean", command)
    _git(root, "config", "filter.audit.smudge", command)
    (root / ".gitattributes").write_text(f"{pattern} filter=audit\n", encoding="utf-8")


def _registry(root: Path):
    registry, _ctx, _store = build_registry(load_repo_config(root))
    return registry


@pytest.fixture
def filtered(sandbox, tmp_path):
    """The sandbox with a filtered, changed .txt file; the filter has not run yet."""
    marker = tmp_path / "marker"
    root = sandbox.root
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    # Tracked: git status filters tracked files only, so an untracked one proves nothing.
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-qm", "notes")
    _install_filter(root, marker)
    (root / "notes.txt").write_text("two\n", encoding="utf-8")
    assert not marker.exists()
    return root, marker


@pytest.mark.parametrize(("tool", "arguments", "action"), [
    ("git_status", {}, "git status"),
    ("git_diff", {}, "a working-tree git diff"),
    ("git_stage", {"paths": ["notes.txt"]}, "git add"),
])
def test_a_worktree_git_tool_refuses_a_filtered_repository_and_runs_nothing(
        filtered, tool, arguments, action):
    root, marker = filtered
    with pytest.raises(ToolError, match=rf"Git filter\(s\) audit.*{action} would run them"):
        _registry(root).get(tool).handler(**arguments)
    assert not marker.exists(), f"{tool} ran the repository's filter"


def test_reads_that_never_touch_the_working_tree_stay_available(filtered):
    root, marker = filtered
    registry = _registry(root)
    assert registry.get("git_diff").handler(staged=True).ok
    assert registry.get("git_log").handler(limit=1).ok
    assert not marker.exists()


def test_a_configured_filter_no_file_uses_does_not_block_status(sandbox, tmp_path):
    marker = tmp_path / "marker"
    _install_filter(sandbox.root, marker, pattern="*.never-used")
    assert _registry(sandbox.root).get("git_status").handler().ok


def test_an_unreadable_filter_answer_is_unknown_and_refused(sandbox, tmp_path, monkeypatch):
    from local_agent.tools import git as git_tools

    _install_filter(sandbox.root, tmp_path / "marker")
    real = git_tools._git_probe

    def truncated(ctx, args, stdin):
        if args[0] == "check-attr":
            return 0, b"", b""
        return real(ctx, args, stdin)

    monkeypatch.setattr(git_tools, "_git_probe", truncated)
    with pytest.raises(ToolError, match="could not be checked.*git status is refused"):
        _registry(sandbox.root).get("git_status").handler()


def test_the_planted_filter_does_run_under_plain_git_status(filtered):
    """Control: plain git status runs this filter, so the refusals above are not vacuous."""
    root, marker = filtered
    subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True,  # noqa: S607
                   capture_output=True)
    assert marker.exists()
