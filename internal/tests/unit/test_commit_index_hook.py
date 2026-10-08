"""Real-Git tests for the private-commit live-index reconciliation transaction."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from local_agent.session.commit_index_hook import (
    IndexLockBusyError,
    Publication,
    prepare_transaction,
    reconcile,
)


def _git(
    root: Path, *args: str, env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, check=True,
    )


def _stage(root: Path, path: str, *, index: Path | None = None) -> bytes:
    env = {"GIT_INDEX_FILE": str(index)} if index is not None else None
    return _git(root, "ls-files", "--stage", "-z", "--", path, env_extra=env).stdout


def _repository(tmp_path: Path) -> tuple[Path, str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "index-test@example.invalid")
    _git(root, "config", "user.name", "Index Test")
    (root / "candidate.txt").write_text("old candidate\n", encoding="utf-8")
    (root / "notes.txt").write_text("old notes\n", encoding="utf-8")
    _git(root, "add", "candidate.txt", "notes.txt")
    _git(root, "commit", "-qm", "base")
    parent = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    live_index_raw = _git(root, "rev-parse", "--git-path", "index").stdout.decode().strip()
    live_index = Path(live_index_raw)
    if not live_index.is_absolute():
        live_index = root / live_index
    return root, parent, live_index


def _branch(root: Path) -> str:
    return _git(root, "symbolic-ref", "HEAD").stdout.decode().strip()


def _candidate_target(root: Path, tmp_path: Path, parent: str) -> bytes:
    private_index = tmp_path / "candidate.index"
    env = {"GIT_INDEX_FILE": str(private_index)}
    _git(root, "read-tree", parent, env_extra=env)
    (root / "candidate.txt").write_text("candidate result\n", encoding="utf-8")
    _git(root, "add", "candidate.txt", env_extra=env)
    return _stage(root, "candidate.txt", index=private_index)


def test_reconcile_advances_clean_candidate_and_preserves_unrelated_staging(tmp_path: Path) -> None:
    root, parent, live_index = _repository(tmp_path)
    expected = _stage(root, "candidate.txt")
    target = _candidate_target(root, tmp_path, parent)

    (root / "notes.txt").write_text("user staged notes\n", encoding="utf-8")
    _git(root, "add", "notes.txt")
    notes_before = _stage(root, "notes.txt")

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    transaction = prepare_transaction(
        workspaces, root, live_index,
        Publication(ref=_branch(root), parent=parent, commit=parent),
        (("candidate.txt", expected, target),),
    )
    reconcile(transaction)

    assert _stage(root, "candidate.txt") == target
    assert _stage(root, "notes.txt") == notes_before


def test_reconcile_never_overwrites_user_staging_on_candidate_path(tmp_path: Path) -> None:
    root, parent, live_index = _repository(tmp_path)
    expected = _stage(root, "candidate.txt")
    target = _candidate_target(root, tmp_path, parent)

    (root / "candidate.txt").write_text("user staged candidate\n", encoding="utf-8")
    _git(root, "add", "candidate.txt")
    user_stage = _stage(root, "candidate.txt")
    assert user_stage != expected
    assert user_stage != target

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    transaction = prepare_transaction(
        workspaces, root, live_index,
        Publication(ref=_branch(root), parent=parent, commit=parent),
        (("candidate.txt", expected, target),),
    )
    reconcile(transaction)

    assert _stage(root, "candidate.txt") == user_stage


def test_reconcile_never_removes_an_index_lock_it_did_not_create(tmp_path: Path) -> None:
    """Deleting another writer's index.lock would let two processes race on the index."""
    root, parent, live_index = _repository(tmp_path)
    expected = _stage(root, "candidate.txt")
    target = _candidate_target(root, tmp_path, parent)
    _git(root, "checkout", "--", "candidate.txt")
    index_bytes = live_index.read_bytes()
    lock = Path(str(live_index) + ".lock")
    lock.write_bytes(b"another writer")

    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    transaction = prepare_transaction(
        workspaces, root, live_index,
        Publication(ref=_branch(root), parent=parent, commit=parent),
        (("candidate.txt", expected, target),),
    )
    with pytest.raises(IndexLockBusyError):
        reconcile(transaction)

    assert lock.read_bytes() == b"another writer"
    assert live_index.read_bytes() == index_bytes
