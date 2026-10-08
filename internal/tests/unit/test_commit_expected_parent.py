"""A candidate commit is published only onto the parent it was built from (#453).

The private tree is ``parent`` plus exactly the candidate paths. Publishing it after the
branch moved would make it the child of newer user work while carrying the older
contents, silently reverting that work in the new HEAD. Publication is one
compare-and-swap ``update-ref`` from ``parent``; anything that moved the branch first
makes it refuse with no ref changed.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from local_agent.session import commit_index_hook
from local_agent.session.workspaces import Committed, CommitRefused
from test_commit_literal_paths import _apply, _committed_paths, _git, _repo


def _state(user: Path) -> tuple[str, ...]:
    return (
        _git(user, "rev-parse", "HEAD"),
        _git(user, "ls-files", "--stage"),
        _git(user, "status", "--porcelain=v1", "--untracked-files=all"),
        (user / "a.txt").read_text(encoding="utf-8"),
        (user / "b.txt").read_text(encoding="utf-8"),
    )


def _user_commits_b_before(manager, user: Path, step: str, monkeypatch) -> list[str]:
    real_git = manager._git
    concurrent: list[str] = []

    def race(cwd, *args, **kwargs):
        if args and args[0] == step and not concurrent:
            (user / "b.txt").write_text("user committed work\n", encoding="utf-8")
            _git(user, "add", "b.txt")
            _git(user, "commit", "-qm", "user concurrent commit")
            concurrent.append(_git(user, "rev-parse", "HEAD").strip())
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", race)
    return concurrent


@pytest.mark.parametrize("step", ["commit-tree", "update-ref"])
def test_a_concurrent_user_commit_is_never_published_over(tmp_path, monkeypatch, step):
    """Astra's #453 interleaving, at the last moment before each publication step."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    concurrent = _user_commits_b_before(manager, user, step, monkeypatch)

    done = manager.commit_applied(task, user, "candidate")

    assert concurrent, "the interleaving never ran"
    assert isinstance(done, CommitRefused), done
    assert "moved while the commit was being prepared" in done.reason
    head = _git(user, "rev-parse", "HEAD").strip()
    assert head == concurrent[0], "a ref advanced past the user's commit"
    assert _git(user, "show", "HEAD:b.txt") == "user committed work\n"
    assert (user / "b.txt").read_text(encoding="utf-8") == "user committed work\n"
    assert (user / "a.txt").read_text(encoding="utf-8") == "candidate\n"
    record = json.loads((manager.workspaces_root / f"{task}.applied.json").read_text(
        encoding="utf-8"))
    assert "committed" not in record
    assert not list(manager.workspaces_root.glob(".commit-index-*.json"))


def test_a_refused_publication_leaves_the_users_state_byte_exact(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    real_git = manager._git
    seen: list[tuple[str, ...]] = []

    def race(cwd, *args, **kwargs):
        if args and args[0] == "update-ref" and not seen:
            (user / "b.txt").write_text("user committed work\n", encoding="utf-8")
            _git(user, "add", "b.txt")
            _git(user, "commit", "-qm", "user concurrent commit")
            seen.append(_state(user))
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", race)
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, CommitRefused), done
    assert _state(user) == seen[0], "the refusal changed the user's HEAD, index or files"


def test_the_refused_candidate_commits_cleanly_on_retry(tmp_path, monkeypatch):
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    concurrent = _user_commits_b_before(manager, user, "update-ref", monkeypatch)
    assert isinstance(manager.commit_applied(task, user, "candidate"), CommitRefused)
    monkeypatch.undo()

    done = manager.commit_applied(task, user, "candidate")

    assert isinstance(done, Committed), done
    assert _git(user, "rev-parse", "HEAD~1").strip() == concurrent[0]
    assert _committed_paths(user) == ["a.txt"]
    assert _git(user, "show", "HEAD:b.txt") == "user committed work\n"


@pytest.mark.parametrize("move", ["switch", "detach"])
def test_a_concurrent_branch_change_never_redirects_publication(tmp_path, monkeypatch, move):
    """Supported contract: publication targets only the branch HEAD named when the commit
    was requested, and only while it still points at the parent. A switch to another
    branch at the same commit, or a detach, never receives the candidate, and the live
    index (now another checkout's) is not advanced."""
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    branch = _git(user, "symbolic-ref", "--short", "HEAD").strip()
    parent = _git(user, "rev-parse", "HEAD").strip()
    real_git = manager._git
    moved: list[str] = []

    def change_branch(cwd, *args, **kwargs):
        if args and args[0] == "update-ref" and not moved:
            if move == "switch":
                _git(user, "switch", "-q", "-c", "elsewhere")
            else:
                _git(user, "switch", "-q", "--detach")
            moved.append(move)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(manager, "_git", change_branch)
    index_before = _git(user, "ls-files", "--stage")
    done = manager.commit_applied(task, user, "candidate")

    assert moved
    assert isinstance(done, Committed), done
    assert done.branch == branch
    assert _git(user, "rev-parse", f"refs/heads/{branch}").strip() == done.commit
    assert _git(user, "rev-parse", "HEAD").strip() == parent, "HEAD followed the commit"
    if move == "switch":
        assert _git(user, "rev-parse", "refs/heads/elsewhere").strip() == parent
    assert done.index_left == ("a.txt",)
    assert _git(user, "ls-files", "--stage") == index_before


def test_the_reference_transaction_hook_reconciles_inside_publication(tmp_path, monkeypatch):
    """The controller's own pass is a no-op here: the hook already advanced the index."""
    import local_agent.session.workspaces as workspaces_module

    calls: list[Path] = []

    def observe(transaction: Path) -> tuple[str, ...]:
        calls.append(transaction)
        return ()

    monkeypatch.setattr(workspaces_module, "reconcile_and_report", observe)
    user = _repo(tmp_path, {"a.txt": "old a\n", "b.txt": "old b\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    done = manager.commit_applied(task, user, "candidate")
    assert isinstance(done, Committed), done
    assert calls, "the controller pass did not run"
    assert _git(user, "rev-parse", ":a.txt") == _git(user, "rev-parse", "HEAD:a.txt")


def test_the_hook_ignores_ref_updates_that_are_not_its_publication(tmp_path):
    """Even with HEAD on the named commit, only the exact ref update it was prepared for
    lets the hook touch the index."""
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    parent = _git(user, "rev-parse", "HEAD").strip()
    branch = _git(user, "symbolic-ref", "HEAD").strip()
    expected = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "a.txt"],  # noqa: S607
                              cwd=user, capture_output=True, check=True).stdout
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=user,  # noqa: S607
                          input=b"other\n", capture_output=True, check=True).stdout.strip()
    target = b"100644 " + blob + b" 0\ta.txt\0"
    workspaces = tmp_path / "ws-hook"
    workspaces.mkdir()
    live = Path(_git(user, "rev-parse", "--git-path", "index").strip())
    transaction = commit_index_hook.prepare_transaction(
        workspaces, user, live if live.is_absolute() else user / live,
        commit_index_hook.Publication(ref=branch, parent=parent, commit=parent),
        (("a.txt", expected, target),),
    )
    index_before = _git(user, "ls-files", "--stage")
    for unrelated in (f"{parent} {parent} refs/heads/elsewhere\n", "", "garbage\n"):
        assert commit_index_hook.main([str(transaction)], updates=unrelated) == 0
        assert _git(user, "ls-files", "--stage") == index_before
    # Control: the matching update does advance it, so the refusals above are not vacuous.
    assert commit_index_hook.main([str(transaction)],
                                  updates=f"{parent} {parent} {branch}\n") == 0
    assert _git(user, "ls-files", "--stage") != index_before


def test_the_message_is_cleaned_as_git_commit_would_and_an_empty_one_is_refused(tmp_path):
    user = _repo(tmp_path, {"a.txt": "old a\n"})
    manager, task = _apply(tmp_path, user, {"a.txt": "candidate\n"})
    raw = "\n  subject  \n\n\n body text \n\n"
    expected = subprocess.run(["git", "stripspace"], cwd=user, input=raw,  # noqa: S607
                              capture_output=True, text=True, check=True).stdout
    done = manager.commit_applied(task, user, raw)
    assert isinstance(done, Committed), done
    assert _git(user, "log", "-1", "--format=%B").rstrip("\n") == expected.rstrip("\n")

    (tmp_path / "second").mkdir()
    user2 = _repo(tmp_path / "second", {"a.txt": "old a\n"})
    manager2, task2 = _apply(tmp_path / "second", user2, {"a.txt": "candidate\n"})
    before = _git(user2, "rev-parse", "HEAD")
    refused = manager2.commit_applied(task2, user2, " \n\n \n")
    assert isinstance(refused, CommitRefused), refused
    assert _git(user2, "rev-parse", "HEAD") == before
