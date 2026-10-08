"""`/commit` commits exactly the candidate's paths, whatever characters they contain (#393).

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

from local_agent.session.workspaces import (
    CommitMismatched,
    Committed,
    CommitRefused,
    GitWorkspaceManager,
)


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
        if args and args[0] == "write-tree":
            private_env = kwargs.get("env_extra")
            assert private_env and "GIT_INDEX_FILE" in private_env
            real_git(cwd, "add", "--", "b.txt", env_extra=private_env)
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
    """git refuses an empty message after the candidate was staged privately."""
    user = _repo(tmp_path, {"a.txt": "old\n", "keep.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n", "a.txt": "candidate\n"})
    (user / "keep.txt").write_text("user staged\n", encoding="utf-8")
    _git(user, "add", "--", "keep.txt")
    head = _git(user, "rev-parse", "HEAD")
    before = (_status(user), _git(user, "ls-files", "--stage"))

    done = manager.commit_applied(task_id, user, "")

    assert isinstance(done, CommitRefused), done
    assert (_status(user), _git(user, "ls-files", "--stage")) == before
    assert _git(user, "rev-parse", "HEAD") == head


def test_refused_commit_restores_user_entry_staged_after_apply(tmp_path):
    """A refused private-index commit cannot replace the user's live index entry."""
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    (user / "new.txt").write_text("USER STAGED CONTENT\n", encoding="utf-8")
    _git(user, "add", "--", "new.txt")
    before = _git(user, "ls-files", "--stage", "--", "new.txt")
    (user / "new.txt").write_text("candidate\n", encoding="utf-8")

    done = manager.commit_applied(task_id, user, "")

    assert isinstance(done, CommitRefused), done
    assert _git(user, "ls-files", "--stage", "--", "new.txt") == before
    assert (user / "new.txt").read_text(encoding="utf-8") == "candidate\n"


def test_refused_commit_preserves_user_staged_identical_candidate_bytes(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    _git(user, "add", "--", "new.txt")
    before = _git(user, "ls-files", "--stage", "--", "new.txt")

    done = manager.commit_applied(task_id, user, "")

    assert isinstance(done, CommitRefused), done
    assert _git(user, "ls-files", "--stage", "--", "new.txt") == before


@pytest.mark.skipif(os.name == "nt", reason="the Windows index has no executable-bit distinction")
def test_refused_commit_restores_index_mode_and_flags(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    (user / "new.txt").write_text("USER STAGED CONTENT\n", encoding="utf-8")
    _git(user, "add", "--", "new.txt")
    _git(user, "update-index", "--chmod=+x", "--assume-unchanged", "--", "new.txt")
    before_stage = _git(user, "ls-files", "--stage", "--", "new.txt")
    before_flags = _git(user, "ls-files", "-v", "--", "new.txt")
    (user / "new.txt").write_text("candidate\n", encoding="utf-8")

    done = manager.commit_applied(task_id, user, "")

    assert isinstance(done, CommitRefused), done
    assert _git(user, "ls-files", "--stage", "--", "new.txt") == before_stage
    assert _git(user, "ls-files", "-v", "--", "new.txt") == before_flags


def test_a_signing_failure_is_refused_with_the_index_restored(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    # A signer that always fails: a genuine git commit failure after private staging.
    _git(user, "config", "commit.gpgsign", "true")
    _git(user, "config", "gpg.format", "openpgp")
    _git(user, "config", "gpg.program", "false")
    before = _status(user)

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, CommitRefused), done
    assert "git commit failed" in done.reason
    assert _status(user) == before


def test_user_restaging_during_failed_private_commit_is_untouched(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    real_git = manager._git

    def user_restages_then_commit_fails(cwd, *args, **kwargs):
        if args and args[0] == "commit-tree":
            (user / "new.txt").write_text("user changed it\n", encoding="utf-8")
            real_git(cwd, "add", "--", "new.txt")
            # A genuine commit-object failure: a tree that does not exist.
            return real_git(cwd, "commit-tree", "0" * 40, "-m", "x", **kwargs)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", user_restages_then_commit_fails)
    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, CommitRefused), done
    assert "A  new.txt" in _git(user, "status", "--porcelain=v1")
    assert (user / "new.txt").read_text(encoding="utf-8") == "user changed it\n"


def test_successful_commit_preserves_user_entry_staged_after_apply(tmp_path):
    """A successful controller commit must not consume the user's staged version."""
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    (user / "new.txt").write_text("USER STAGED CONTENT\n", encoding="utf-8")
    _git(user, "add", "--", "new.txt")
    before = _git(user, "ls-files", "--stage", "--", "new.txt")
    (user / "new.txt").write_text("candidate\n", encoding="utf-8")

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, Committed), done
    assert _git(user, "show", "HEAD:new.txt") == "candidate\n"
    assert _git(user, "ls-files", "--stage", "--", "new.txt") == before
    assert "new.txt" in _git(user, "diff", "--cached", "--name-only")
    assert (user / "new.txt").read_text(encoding="utf-8") == "candidate\n"


def test_successful_commit_of_unstaged_new_file_leaves_a_clean_index(tmp_path):
    """Preserving user staging must not manufacture a staged deletion after success."""
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, Committed), done
    assert done.index_left == ()
    assert _git(user, "show", "HEAD:new.txt") == "candidate\n"
    assert _status(user) == ""


def test_successful_commit_of_clean_tracked_file_leaves_a_clean_index(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"a.txt": "candidate\n"})

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, Committed), done
    assert done.index_left == ()
    assert _git(user, "show", "HEAD:a.txt") == "candidate\n"
    assert _status(user) == ""


def test_commit_refuses_an_unmerged_index_without_writing(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    blob = _git(user, "rev-parse", "HEAD:a.txt").strip()
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=str(user),
        check=True,
        input=(
            b"0 0000000000000000000000000000000000000000\ta.txt\n"
            + f"100644 {blob} 1\ta.txt\n".encode("ascii")
            + f"100644 {blob} 2\ta.txt\n".encode("ascii")
        ),
        capture_output=True,
    )
    unmerged = _git(user, "ls-files", "--unmerged", "--", "a.txt")
    assert " 1\ta.txt" in unmerged and " 2\ta.txt" in unmerged
    head = _git(user, "rev-parse", "HEAD")

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, CommitRefused), done
    assert "unresolved merge entries" in done.reason
    assert _git(user, "rev-parse", "HEAD") == head


def _live_index_lock(user: Path) -> Path:
    raw = Path(_git(user, "rev-parse", "--git-path", "index").strip())
    index = raw if raw.is_absolute() else user / raw
    return Path(str(index) + ".lock")


def test_commit_while_another_git_holds_the_index_lock_reports_and_never_breaks_it(tmp_path):
    """A concurrent Git process owns index.lock for the whole commit attempt.

    LCA must not delete or write through that lock. The commit itself is exact; the
    clean candidate path is reported as not reconciled and the live index is untouched.
    """
    user = _repo(tmp_path, {"a.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"new.txt": "candidate\n"})
    index_before = _git(user, "ls-files", "--stage")
    lock = _live_index_lock(user)
    lock.write_bytes(b"held by another git process")

    try:
        done = manager.commit_applied(task_id, user, "candidate")
        assert lock.read_bytes() == b"held by another git process"
    finally:
        lock.unlink(missing_ok=True)

    assert isinstance(done, Committed), done
    assert _git(user, "show", "HEAD:new.txt") == "candidate\n"
    assert done.index_left == ("new.txt",)
    assert _git(user, "ls-files", "--stage") == index_before


def test_controller_pass_reconciles_under_the_lock_when_the_hook_did_not_run(
    tmp_path, monkeypatch,
):
    """Hook absent (interpreter gone, killed controller): the controller's own pass
    advances the clean path through the same locked transaction, not a bare rewrite."""
    import local_agent.session.workspaces as workspaces_module

    monkeypatch.setattr(workspaces_module, "hook_environment", lambda _transaction: {})
    user = _repo(tmp_path, {"a.txt": "old\n", "keep.txt": "old\n"})
    manager, task_id = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    (user / "keep.txt").write_text("user staged\n", encoding="utf-8")
    _git(user, "add", "--", "keep.txt")
    keep_before = _git(user, "ls-files", "--stage", "--", "keep.txt")

    done = manager.commit_applied(task_id, user, "candidate")

    assert isinstance(done, Committed), done
    assert done.index_left == ()
    assert _git(user, "rev-parse", ":a.txt") == _git(user, "rev-parse", "HEAD:a.txt")
    assert _git(user, "ls-files", "--stage", "--", "keep.txt") == keep_before
    assert _git(user, "diff", "--cached", "--name-only") == "keep.txt\n"
