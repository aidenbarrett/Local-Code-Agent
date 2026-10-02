"""git_status reports conflicts, interrupted operations and divergence instead of hiding them.

Before this, unmerged index entries were dropped by the parser, so a repository
stopped mid-merge reported "0 changed file(s)" and nothing about the merge.
"""
from __future__ import annotations

import hashlib
import subprocess

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.git import _parse_porcelain_v2


def _git(root, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
        cwd=root, check=check, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _status(sandbox):
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    return registry.get("git_status").handler()


def _write(sandbox, name: str, text: str) -> None:
    (sandbox.root / name).write_text(text, encoding="utf-8")


def _commit(sandbox, name: str, text: str, message: str) -> None:
    _write(sandbox, name, text)
    _git(sandbox.root, "add", name)
    _git(sandbox.root, "commit", "-qm", message)


def _diverged(sandbox) -> None:
    """main and other both change conflict.txt differently from a shared base."""
    _git(sandbox.root, "branch", "-M", "main")
    _commit(sandbox, "conflict.txt", "base\n", "base")
    _git(sandbox.root, "switch", "-q", "-c", "other")
    _commit(sandbox, "conflict.txt", "theirs\n", "theirs")
    _git(sandbox.root, "switch", "-q", "main")
    _commit(sandbox, "conflict.txt", "ours\n", "ours")


def _index_and_head(sandbox) -> tuple[str, str]:
    index = (sandbox.root / ".git" / "index").read_bytes()
    return hashlib.sha256(index).hexdigest(), _git(sandbox.root, "rev-parse", "HEAD")


def test_a_clean_repository_reports_no_operation_and_no_conflicts(sandbox):
    data = _status(sandbox).data
    assert data["operation"] is None and data["bisecting"] is False
    assert data["operation_commands"] is None
    assert data["conflicted"] == []


def test_a_stopped_merge_names_the_operation_and_the_conflicted_file(sandbox):
    _diverged(sandbox)
    _git(sandbox.root, "merge", "other", check=False)
    before = _index_and_head(sandbox)
    result = _status(sandbox)
    assert result.data["operation"] == "merge"
    assert result.data["operation_commands"] == {"continue": "git merge --continue",
                                                 "abort": "git merge --abort"}
    assert result.data["conflicted"] == [{"path": "conflict.txt", "state": "both modified"}]
    assert "1 conflicted" in result.summary and "a merge is in progress" in result.summary
    # Reading the state must not resolve, stage or abort anything.
    assert _index_and_head(sandbox) == before
    assert (sandbox.root / ".git" / "MERGE_HEAD").exists()


def test_a_stopped_rebase_is_reported_as_a_rebase(sandbox):
    _diverged(sandbox)
    _git(sandbox.root, "switch", "-q", "other")
    _git(sandbox.root, "rebase", "main", check=False)
    data = _status(sandbox).data
    assert data["operation"] == "rebase"
    assert data["operation_commands"]["abort"] == "git rebase --abort"
    assert [entry["path"] for entry in data["conflicted"]] == ["conflict.txt"]


@pytest.mark.parametrize("command, operation", [("cherry-pick", "cherry-pick"), ("revert", "revert")])
def test_stopped_single_commit_operations_are_named(sandbox, command, operation):
    _diverged(sandbox)
    # Picking "theirs", or reverting the commit that created the file under a later
    # edit of it, both stop on conflict.txt.
    target = "other" if command == "cherry-pick" else "HEAD~1"
    _git(sandbox.root, command, target, check=False)
    data = _status(sandbox).data
    assert data["operation"] == operation
    assert data["conflicted"], data


def test_a_deleted_by_them_conflict_uses_gits_own_terms(sandbox):
    _git(sandbox.root, "branch", "-M", "main")
    _commit(sandbox, "conflict.txt", "base\n", "base")
    _git(sandbox.root, "switch", "-q", "-c", "other")
    _git(sandbox.root, "rm", "-q", "conflict.txt")
    _git(sandbox.root, "commit", "-qm", "delete")
    _git(sandbox.root, "switch", "-q", "main")
    _commit(sandbox, "conflict.txt", "ours\n", "ours")
    _git(sandbox.root, "merge", "other", check=False)
    data = _status(sandbox).data
    assert data["conflicted"] == [{"path": "conflict.txt", "state": "deleted by them"}]


def test_bisect_is_reported_separately_from_operations(sandbox):
    _diverged(sandbox)
    _git(sandbox.root, "bisect", "start")
    data = _status(sandbox).data
    assert data["bisecting"] is True and data["operation"] is None
    assert "a bisect is in progress" in _status(sandbox).summary


def test_ahead_and_behind_upstream_are_numbers(sandbox):
    _diverged(sandbox)
    _git(sandbox.root, "branch", "--set-upstream-to=other", "main")
    result = _status(sandbox)
    assert result.data["upstream_divergence"] == {"ahead": 1, "behind": 1}
    assert "(1 ahead, 1 behind upstream)" in result.summary


def test_no_upstream_means_unknown_divergence_not_zero(sandbox):
    assert _status(sandbox).data["upstream_divergence"] is None


def test_the_parser_keeps_unmerged_paths_with_spaces():
    record = "u UU N... 100644 100644 100644 100644 " + " ".join(["a" * 40] * 3) + " dir/my file.c"
    data = _parse_porcelain_v2(record + "\0")
    assert data["conflicted"] == [{"path": "dir/my file.c", "state": "both modified"}]
    assert data["changed"] == []
