"""Candidate worktrees: LCA edits its own copy, and import into the user's checkout is exact."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout


def _user_repo(tmp_path: Path) -> Path:
    root = tmp_path / "user"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.cpp").write_text("int a() { return 1; }\n", encoding="utf-8")
    (root / "src" / "b.cpp").write_text("int b() { return 2; }\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _state(root: Path) -> tuple[str, str, str]:
    """What the user can see: status, staged diff and unstaged diff."""
    return (
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        _git(root, "diff", "--cached"),
        _git(root, "diff"),
    )


def _manager(tmp_path: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "lca-workspaces", controller_commit="c" * 40)


def test_dirty_tracked_state_is_the_base_and_the_user_checkout_is_untouched(tmp_path):
    user = _user_repo(tmp_path)
    (user / "src" / "a.cpp").write_text("int a() { return 10; }\n", encoding="utf-8")
    (user / "src" / "b.cpp").write_text("int b() { return 20; }\n", encoding="utf-8")
    _git(user, "add", "src/b.cpp")  # one staged, one unstaged edit
    (user / "notes.txt").write_text("secret scratch\n", encoding="utf-8")
    before = _state(user)
    stash_before = _git(user, "stash", "list")

    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        assert _state(user) == before
        assert _git(user, "stash", "list") == stash_before
        assert ws.base_commit != ws.head_commit
        assert set(ws.dirty_paths) == {"src/a.cpp", "src/b.cpp"}
        assert ws.untracked_excluded == ("notes.txt",)
        assert (ws.root / "src" / "a.cpp").read_text(encoding="utf-8") == "int a() { return 10; }\n"
        assert (ws.root / "src" / "b.cpp").read_text(encoding="utf-8") == "int b() { return 20; }\n"
        assert not (ws.root / "notes.txt").exists()
        assert not ws.root.is_relative_to(user)
    finally:
        manager.close(ws)


def test_clean_checkout_bases_on_head(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        assert ws.base_commit == ws.head_commit
        assert ws.dirty_paths == ()
    finally:
        manager.close(ws)


def test_touched_but_unchanged_files_base_on_head(tmp_path):
    # A build or an editor save can leave files stat-dirty with identical content. With
    # optional locks off, `git stash create` then exits 1 with no output.
    user = _user_repo(tmp_path)
    target = user / "src" / "a.cpp"
    content = target.read_bytes()
    stat = target.stat()
    target.write_bytes(content)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        assert ws.base_commit == ws.head_commit
        assert ws.dirty_paths == ()
    finally:
        manager.close(ws)


def test_user_hooks_never_run_for_controller_git_operations(tmp_path):
    user = _user_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hooks = user / ".git" / "hooks"
    for name in ("post-checkout", "pre-commit", "post-index-change"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\n", encoding="utf-8")
        hook.chmod(0o755)
    _git(user, "config", "core.hooksPath", str(hooks))

    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "src" / "a.cpp").write_text("int a() { return 3; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        assert manager.import_patch(ws, candidate).applied
    finally:
        manager.close(ws)
    assert not marker.exists()


def test_candidate_excludes_build_and_run_dirs_and_records_new_files(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()), excluded_dirs=("out", ".local-agent"))
    try:
        (ws.root / "src" / "a.cpp").write_text("int a() { return 4; }\n", encoding="utf-8")
        (ws.root / "src" / "c.cpp").write_text("int c() { return 5; }\n", encoding="utf-8")
        (ws.root / "out").mkdir()
        (ws.root / "out" / "a.o").write_bytes(b"\x7fELF")
        (ws.root / ".local-agent" / "runs").mkdir(parents=True)
        (ws.root / ".local-agent" / "runs" / "log.txt").write_text("x", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        assert candidate.paths == ("src/a.cpp", "src/c.cpp")
        assert b"out/a.o" not in candidate.patch
        assert b".local-agent" not in candidate.patch
    finally:
        manager.close(ws)


def test_ignored_build_dir_and_base_tracked_files_under_excluded_dirs(tmp_path):
    # The real layout: build/ is gitignored, and git refuses exclude pathspecs that name
    # ignored directories. A file the user tracks under an excluded dir must not show up
    # as deleted either.
    user = _user_repo(tmp_path)
    (user / "tools").mkdir()
    (user / "tools" / "keep.txt").write_text("tracked\n", encoding="utf-8")
    _git(user, "add", "-A")
    _git(user, "commit", "-qm", "tracked under an excluded dir")
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()), excluded_dirs=("build", "tools"))
    try:
        (ws.root / "build").mkdir()
        (ws.root / "build" / "a.o").write_bytes(b"obj")
        (ws.root / "tools" / "keep.txt").unlink()
        (ws.root / "tools" / "generated.txt").write_text("x", encoding="utf-8")
        (ws.root / "src" / "a.cpp").write_text("int a() { return 6; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        assert candidate.paths == ("src/a.cpp",)
    finally:
        manager.close(ws)


def test_import_applies_exactly_without_touching_the_index(tmp_path):
    user = _user_repo(tmp_path)
    (user / "src" / "b.cpp").write_text("int b() { return 7; }\n", encoding="utf-8")  # unrelated dirty edit
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "src" / "a.cpp").write_text("int a() { return 42; }\n", encoding="utf-8")
        (ws.root / "src" / "new.cpp").write_text("int n() { return 0; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        staged_before = _git(user, "diff", "--cached")

        result = manager.import_patch(ws, candidate)

        assert result.applied and result.verified
        assert result.conflicts == ()
        assert (user / "src" / "a.cpp").read_text(encoding="utf-8").replace("\r\n", "\n") == "int a() { return 42; }\n"
        assert (user / "src" / "new.cpp").exists()
        # The unrelated dirty edit survives and nothing was staged.
        assert (user / "src" / "b.cpp").read_text(encoding="utf-8").replace("\r\n", "\n") == "int b() { return 7; }\n"
        assert _git(user, "diff", "--cached") == staged_before
        assert dict(result.pre_blobs)["src/new.cpp"] is None
    finally:
        manager.close(ws)


def test_outside_edit_to_a_touched_file_refuses_the_whole_import(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "src" / "a.cpp").write_text("int a() { return 99; }\n", encoding="utf-8")
        (ws.root / "src" / "b.cpp").write_text("int b() { return 99; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        # The user keeps working while the candidate is prepared.
        (user / "src" / "b.cpp").write_text("int b() { return -1; }\n", encoding="utf-8")
        before = _state(user)

        result = manager.import_patch(ws, candidate)

        assert result.applied is False
        assert result.conflicts == ("src/b.cpp",)
        assert _state(user) == before, "a refused import wrote to the user's checkout"
        assert (user / "src" / "a.cpp").read_text(encoding="utf-8").replace("\r\n", "\n") == "int a() { return 1; }\n"
    finally:
        manager.close(ws)


def test_user_creating_a_file_the_candidate_adds_is_a_conflict(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "src" / "new.cpp").write_text("int n() { return 1; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        (user / "src" / "new.cpp").write_text("mine\n", encoding="utf-8")
        result = manager.import_patch(ws, candidate)
        assert result.applied is False
        assert result.conflicts == ("src/new.cpp",)
        assert (user / "src" / "new.cpp").read_text(encoding="utf-8") == "mine\n"
    finally:
        manager.close(ws)


def test_tampered_or_foreign_candidates_are_rejected(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    first = manager.create(user, str(uuid4()))
    second = manager.create(user, str(uuid4()))
    try:
        (first.root / "src" / "a.cpp").write_text("int a() { return 5; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(first)
        with pytest.raises(WorkspaceError):
            manager.import_patch(second, candidate)
        from dataclasses import replace
        with pytest.raises(WorkspaceError):
            manager.import_patch(first, replace(candidate, patch=candidate.patch + b"\n"))
    finally:
        manager.close(first)
        manager.close(second)


def test_one_workspace_lease_per_task_and_close_releases_it(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    task_id = str(uuid4())
    ws = manager.create(user, task_id)
    with pytest.raises(WorkspaceError):
        manager.create(user, task_id)
    manager.close(ws)
    assert not ws.root.exists()
    assert str(ws.root) not in _git(user, "worktree", "list", "--porcelain")
    again = manager.create(user, task_id)
    manager.close(again)


def test_workspaces_root_inside_the_user_checkout_is_refused(tmp_path):
    user = _user_repo(tmp_path)
    manager = GitWorkspaceManager(user / ".lca-ws", controller_commit="c" * 40)
    with pytest.raises(WorkspaceError):
        manager.create(user, str(uuid4()))


def test_empty_candidate_is_not_imported(tmp_path):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        result = manager.import_patch(ws, manager.candidate_patch(ws))
        assert result.applied is False
        assert result.refused_reason == "candidate changes nothing"
    finally:
        manager.close(ws)


@pytest.mark.skipif(os.name != "nt", reason="exercises autocrlf checkouts on Windows")
def test_crlf_checkout_is_not_mistaken_for_a_conflict(tmp_path):
    user = tmp_path / "crlf"
    user.mkdir()
    _git(user, "init", "-q")
    _git(user, "config", "core.autocrlf", "true")
    (user / "a.txt").write_bytes(b"one\ntwo\n")
    _git(user, "add", "-A")
    _git(user, "commit", "-qm", "base")
    (user / "a.txt").unlink()
    _git(user, "checkout", "--", "a.txt")
    assert b"\r\n" in (user / "a.txt").read_bytes()

    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "a.txt").write_text("one\nTWO\n", encoding="utf-8")
        result = manager.import_patch(ws, manager.candidate_patch(ws))
        assert result.applied and result.verified, result
    finally:
        manager.close(ws)


def test_partial_import_restores_modified_files_and_removes_created_ones(tmp_path, monkeypatch):
    user = _user_repo(tmp_path)
    manager = _manager(tmp_path)
    ws = manager.create(user, str(uuid4()))
    try:
        (ws.root / "src" / "a.cpp").write_text("int a() { return 8; }\n", encoding="utf-8")
        (ws.root / "src" / "new.cpp").write_text("int n() { return 8; }\n", encoding="utf-8")
        candidate = manager.candidate_patch(ws)
        before = _state(user)
        a_before = (user / "src" / "a.cpp").read_bytes()
        real = manager._git

        def faulty(cwd, *args, **kwargs):
            if args and args[0] == "apply" and "--check" not in args:
                (user / "src" / "a.cpp").write_bytes((ws.root / "src" / "a.cpp").read_bytes())
                (user / "src" / "new.cpp").write_bytes((ws.root / "src" / "new.cpp").read_bytes())
                return subprocess.CompletedProcess(["git", *args], 128, b"", b"injected")
            return real(cwd, *args, **kwargs)

        monkeypatch.setattr(manager, "_git", faulty)
        result = manager.import_patch(ws, candidate)

        assert result.applied is False and result.rolled_back is True
        assert result.unresolved == ()
        assert (user / "src" / "a.cpp").read_bytes() == a_before
        assert not (user / "src" / "new.cpp").exists()
        assert _state(user) == before
    finally:
        monkeypatch.undo()
        manager.close(ws)
