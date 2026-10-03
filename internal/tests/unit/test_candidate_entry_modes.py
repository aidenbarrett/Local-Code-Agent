"""Apply and undo carry file content only; mode and type changes are refused up front (#396).

Before this, a candidate that changed base.txt's bytes and made it executable was
applied, and /undo reported success after restoring the bytes while the file stayed
100755 (`git diff --summary`: mode change 100644 => 100755).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
from uuid import uuid4

import pytest

from local_agent.session.workspaces import GitWorkspaceManager

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _state(user: Path) -> tuple[str, str]:
    return (_git(user, "status", "--porcelain=v1", "--untracked-files=all"),
            _git(user, "diff", "--summary"))


def _candidate(tmp_path: Path, user: Path, change):
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
    task_id = str(uuid4())
    ws = manager.create(user, task_id)
    change(ws.root)
    return manager, task_id, ws, manager.candidate_patch(ws)


def _chmod_x(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_a_content_and_mode_change_is_refused_before_anything_is_written(tmp_path):
    user = _repo(tmp_path)

    def change(root: Path) -> None:
        (root / "base.txt").write_text("changed\n", encoding="utf-8")
        _chmod_x(root / "base.txt")

    manager, _task, ws, candidate = _candidate(tmp_path, user, change)
    before = _state(user)
    result = manager.import_patch(ws, candidate)
    assert result.applied is False
    assert "permissions or file types" in (result.refused_reason or "")
    assert "mode change 100644 => 100755 base.txt" in (result.refused_reason or "")
    assert _state(user) == before
    assert (user / "base.txt").read_text(encoding="utf-8") == "base\n"


def test_a_mode_only_change_is_refused(tmp_path):
    user = _repo(tmp_path)
    manager, _task, ws, candidate = _candidate(
        tmp_path, user, lambda root: _chmod_x(root / "base.txt"))
    result = manager.import_patch(ws, candidate)
    assert result.applied is False and "base.txt" in (result.refused_reason or "")


def test_a_symlink_is_refused(tmp_path):
    user = _repo(tmp_path)
    manager, _task, ws, candidate = _candidate(
        tmp_path, user, lambda root: (root / "link").symlink_to("base.txt"))
    result = manager.import_patch(ws, candidate)
    assert result.applied is False and "create mode 120000 link" in (result.refused_reason or "")


def test_content_only_apply_and_undo_restore_the_exact_tree_entry(tmp_path):
    user = _repo(tmp_path)

    def change(root: Path) -> None:
        (root / "base.txt").write_text("changed\n", encoding="utf-8")
        (root / "tool.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        _chmod_x(root / "tool.sh")  # a NEW executable file is plain content plus mode

    manager, task_id, ws, candidate = _candidate(tmp_path, user, change)
    before = _state(user)
    result = manager.import_patch(ws, candidate)
    assert result.applied and result.verified, result.refused_reason
    manager.record_applied(task_id, user, candidate, result)
    manager.discard(ws)

    undone = manager.undo_applied(task_id, user)

    assert undone.undone, undone
    assert _state(user) == before  # no stray mode change, no leftover file
    assert not (user / "tool.sh").exists()


def test_undo_leaves_a_later_user_chmod_alone(tmp_path):
    user = _repo(tmp_path)
    manager, task_id, ws, candidate = _candidate(
        tmp_path, user, lambda root: (root / "base.txt").write_text("changed\n", encoding="utf-8"))
    result = manager.import_patch(ws, candidate)
    manager.record_applied(task_id, user, candidate, result)
    manager.discard(ws)
    _chmod_x(user / "base.txt")  # the user's own later permission change

    undone = manager.undo_applied(task_id, user)

    assert undone.undone, undone
    assert (user / "base.txt").read_text(encoding="utf-8") == "base\n"
    assert os.access(user / "base.txt", os.X_OK), "undo overwrote the user's chmod"


def _repo_with_tool(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "tool.sh").write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
    _chmod_x(root / "tool.sh")
    _git(root, "add", "tool.sh")
    _git(root, "commit", "-qm", "tool")
    return root


def test_undo_restores_a_deleted_executable_with_its_mode(tmp_path):
    """Sol's #407 review: undo recreated a deleted 100755 file as 0644."""
    user = _repo_with_tool(tmp_path)
    mode = (user / "tool.sh").stat().st_mode
    manager, task_id, ws, candidate = _candidate(
        tmp_path, user, lambda root: (root / "tool.sh").unlink())
    before = _state(user)
    result = manager.import_patch(ws, candidate)
    assert result.applied and result.verified, result.refused_reason
    assert not (user / "tool.sh").exists()
    manager.record_applied(task_id, user, candidate, result)
    manager.discard(ws)

    undone = manager.undo_applied(task_id, user)

    assert undone.undone, undone
    assert _state(user) == before, "mode change left behind"
    assert stat.S_IMODE((user / "tool.sh").stat().st_mode) == stat.S_IMODE(mode)


def test_a_failed_apply_restores_a_deleted_executable_with_its_mode(tmp_path, monkeypatch):
    user = _repo_with_tool(tmp_path)
    mode = stat.S_IMODE((user / "tool.sh").stat().st_mode)

    def change(root: Path) -> None:
        (root / "tool.sh").unlink()
        (root / "base.txt").write_text("changed\n", encoding="utf-8")

    manager, _task, ws, candidate = _candidate(tmp_path, user, change)
    before = _state(user)
    real = manager._git

    def faulty(cwd, *args, **kwargs):
        if args and args[0] == "apply" and not {"--check", "--summary"} & set(args):
            (user / "tool.sh").unlink()  # git deleted it, then failed
            return subprocess.CompletedProcess(["git", *args], 1, b"", b"injected failure")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", faulty)
    result = manager.import_patch(ws, candidate)

    assert result.applied is False and result.rolled_back, result
    assert result.unresolved == ()
    assert _state(user) == before
    assert stat.S_IMODE((user / "tool.sh").stat().st_mode) == mode


def test_undo_of_a_record_without_modes_is_refused_and_writes_nothing(tmp_path):
    user = _repo_with_tool(tmp_path)
    manager, task_id, ws, candidate = _candidate(
        tmp_path, user, lambda root: (root / "tool.sh").unlink())
    result = manager.import_patch(ws, candidate)
    record = manager.record_applied(task_id, user, candidate, result)
    manager.discard(ws)
    data = json.loads(record.read_text(encoding="utf-8"))
    del data["pre_modes"], data["post_modes"]
    record.write_text(json.dumps(data), encoding="utf-8")
    after_apply = _state(user)

    undone = manager.undo_applied(task_id, user)

    assert undone.undone is False
    assert "permissions were kept" in (undone.refused_reason or "")
    assert _state(user) == after_apply and not (user / "tool.sh").exists()
