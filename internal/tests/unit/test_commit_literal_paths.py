"""/commit commits exactly the candidate's paths, whatever characters they contain (#393).

Git reads `note[1].txt` after `--` as a pattern that also matches `note1.txt`, so
`commit --only -- note[1].txt` committed an unrelated edit to `note1.txt` and the
controller reported an exact commit.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from local_agent.session.workspaces import CommitMismatched, Committed, CommitRefused, GitWorkspaceManager


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "user"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _apply(tmp_path: Path, user: Path, writes: dict[str, str]):
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
    task_id = str(uuid4())
    ws = manager.create(user, task_id)
    for name, text in writes.items():
        (ws.root / name).write_text(text, encoding="utf-8")
    candidate = manager.candidate_patch(ws)
    result = manager.import_patch(ws, candidate)
    assert result.applied and result.verified
    manager.record_applied(task_id, user, candidate, result)
    manager.discard(ws)
    return manager, task_id


def _committed_paths(user: Path) -> list[str]:
    out = _git(user, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD")
    return sorted(p for p in out.split("\0") if p)


MAGIC_NAMES = ["note[1].txt", "-leading.txt", "with space.txt", "unicodé.txt"]
if os.name != "nt":
    MAGIC_NAMES.append("*.txt")  # '*' is not a legal Windows filename


@pytest.mark.parametrize("created", MAGIC_NAMES)
def test_commit_contains_exactly_the_candidate_and_leaves_matching_user_work(tmp_path, created):
    user = _repo(tmp_path, {"note1.txt": "old\n", "keep.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {created: "candidate\n"})
    # Unrelated work whose names a pattern would match: one unstaged, one staged.
    (user / "note1.txt").write_text("user unstaged edit\n", encoding="utf-8")
    (user / "keep.txt").write_text("user staged edit\n", encoding="utf-8")
    _git(user, "add", "--", "keep.txt")
    index_before = _git(user, "ls-files", "--stage", "--", "keep.txt", "note1.txt")

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, Committed), done
    assert _committed_paths(user) == [created]
    assert _git(user, "ls-files", "--stage", "--", "keep.txt", "note1.txt") == index_before
    assert (user / "note1.txt").read_text(encoding="utf-8") == "user unstaged edit\n"
    assert "keep.txt" in _git(user, "diff", "--cached", "--name-only")


def test_a_commit_whose_changed_paths_exceed_the_candidate_is_a_mismatch(tmp_path, monkeypatch):
    """Post-effect check: the complete committed delta must equal the reviewed scope."""
    user = _repo(tmp_path, {"a.txt": "old\n", "b.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    (user / "b.txt").write_text("user edit\n", encoding="utf-8")
    real_git = manager._git

    def widen(cwd, *args, **kwargs):
        if args and args[0] == "commit":
            args = (*args, "b.txt")  # simulate any route by which extra paths get in
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", widen)
    done = manager.commit_applied(task_id, user, "candidate")
    assert isinstance(done, CommitMismatched), done
    assert _committed_paths(user) == ["a.txt", "b.txt"]


def test_git_stage_tool_stages_only_the_named_file(sandbox):
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    (sandbox.root / "note1.txt").write_text("unrelated\n", encoding="utf-8")
    (sandbox.root / "note[1].txt").write_text("named\n", encoding="utf-8")
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    registry.get("git_stage").handler(paths=["note[1].txt"])
    staged = _git(sandbox.root, "diff", "--cached", "--name-only", "-z").split("\0")
    assert [p for p in staged if p] == ["note[1].txt"]


# ------------------------------------------------------------------ #395


def _status(user: Path) -> str:
    return _git(user, "status", "--porcelain=v1", "-z", "--untracked-files=all")


def test_a_refused_commit_leaves_the_users_index_exactly_as_it_was(tmp_path):
    """git refuses an empty message after the created file was staged."""
    user = _repo(tmp_path, {"a.txt": "old\n", "keep.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n", "a.txt": "candidate\n"})
    (user / "keep.txt").write_text("user staged\n", encoding="utf-8")
    _git(user, "add", "--", "keep.txt")
    head = _git(user, "rev-parse", "HEAD")
    before = (_status(user), _git(user, "ls-files", "--stage"))

    done = manager.commit_applied(task_id, user, "")

    assert isinstance(done, CommitRefused), done
    assert done.index_left == ()
    assert (_status(user), _git(user, "ls-files", "--stage")) == before
    assert _git(user, "rev-parse", "HEAD") == head


def test_a_signing_failure_is_refused_with_the_index_restored(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    # A signer that always fails: a genuine git commit failure after staging.
    _git(user, "config", "commit.gpgsign", "true")
    _git(user, "config", "gpg.format", "openpgp")
    _git(user, "config", "gpg.program", "false")
    before = _status(user)

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, CommitRefused), done
    assert "git commit failed" in done.reason
    assert _status(user) == before


def test_a_staged_entry_changed_meanwhile_is_left_and_reported(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    real_git = manager._git

    def user_restages_then_commit_fails(cwd, *args, **kwargs):
        if args and args[0] == "commit":
            (user / "new.txt").write_text("user changed it\n", encoding="utf-8")
            real_git(cwd, "add", "--", "new.txt")
            # A pathspec git cannot match makes the commit fail, as a real failure would.
            return real_git(cwd, "commit", "--only", "-F", "-", "--", "no-such-file", **kwargs)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", user_restages_then_commit_fails)
    done = manager.commit_applied(task_id, user, "candidate")
    assert isinstance(done, CommitRefused), done
    assert done.index_left == ("new.txt",)
    assert "still staged" in done.reason
    assert "A  new.txt" in _git(user, "status", "--porcelain=v1")
