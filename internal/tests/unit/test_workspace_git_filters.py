"""Candidate checks never run a program named in Git configuration (#398).

`_worktree_blob` used `hash-object --path`, which runs a configured clean filter: a
precondition check executed a repository-configured program.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from local_agent.session.workspaces import (
    CommitRefused,
    GitWorkspaceManager,
    WorkspaceError,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    (root / "code.cpp").write_text("int x;\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _install_filter(root: Path, marker: Path) -> None:
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
    (root / ".gitattributes").write_text("*.txt filter=audit\n", encoding="utf-8")


def _manager(tmp_path: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)


def test_a_repository_using_a_filter_is_not_ready_and_nothing_runs(tmp_path):
    root = _repo(tmp_path)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    manager = _manager(tmp_path)

    readiness = manager.readiness(root)

    assert not readiness.ready
    assert any("Git filter(s) audit" in problem for problem in readiness.problems)
    with pytest.raises(WorkspaceError, match="audit"):
        manager.create(root, str(uuid4()))
    assert not marker.exists(), "a candidate check ran the repository's filter"


def test_apply_undo_and_commit_refuse_once_a_filter_appears_without_running_it(tmp_path):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    task_id = str(uuid4())
    ws = manager.create(root, task_id)
    (ws.root / "base.txt").write_text("changed\n", encoding="utf-8")
    candidate = manager.candidate_patch(ws)
    marker = tmp_path / "marker"
    _install_filter(root, marker)  # the user configures a filter after preparation

    imported = manager.import_patch(ws, candidate)
    assert imported.applied is False and "audit" in (imported.refused_reason or "")
    assert (root / "base.txt").read_text(encoding="utf-8") == "base\n"
    assert not marker.exists()
    manager.close(ws)


def test_commit_and_undo_refuse_when_a_filter_is_added_after_apply(tmp_path):
    root = _repo(tmp_path)
    manager = _manager(tmp_path)
    task_id = str(uuid4())
    ws = manager.create(root, task_id)
    (ws.root / "code.cpp").write_text("int y;\n", encoding="utf-8")
    candidate = manager.candidate_patch(ws)
    result = manager.import_patch(ws, candidate)
    manager.record_applied(task_id, root, candidate, result)
    manager.discard(ws)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-qm", "attributes", "--no-verify")
    marker.unlink(missing_ok=True)  # the user's own commit may run it; ours must not

    committed = manager.commit_applied(task_id, root, "candidate")
    undone = manager.undo_applied(task_id, root)

    assert isinstance(committed, CommitRefused) and "audit" in committed.reason
    assert undone.undone is False and "audit" in (undone.refused_reason or "")
    assert not marker.exists()


def test_a_configured_but_unused_filter_and_autocrlf_stay_supported(tmp_path):
    """A global git-lfs style filter that no file here uses is not a reason to refuse."""
    root = _repo(tmp_path)
    marker = tmp_path / "marker"
    _install_filter(root, marker)
    (root / ".gitattributes").unlink()
    _git(root, "config", "core.autocrlf", "input")
    manager = _manager(tmp_path)
    assert manager.filter_drivers_in_use(root) == ()
    assert manager.readiness(root).ready, manager.readiness(root).problems
    ws = manager.create(root, str(uuid4()))
    manager.close(ws)
    assert not marker.exists()


def test_a_repository_without_filters_is_unaffected(tmp_path):
    root = _repo(tmp_path)
    assert _manager(tmp_path).filter_drivers_in_use(root) == ()
