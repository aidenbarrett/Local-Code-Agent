"""'What's wrong with my repo?' is answered from git status by the controller (#464).

A fixed plan runs the existing read-only git_status tool and explains what it saw,
with next steps for the user to run. No model is called, nothing in the repository
changes, and the advice never runs continue, abort, reset or push.
"""
from __future__ import annotations

import hashlib
import subprocess
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import (
    RULE_GIT_REVIEW,
    RULE_REPOSITORY_STATE,
    RouteAction,
    decide_route,
)
from local_agent.session.repository_state import (
    REPOSITORY_STATE,
    explain_repository_state,
    repository_state_sha256,
)
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager


def _status(**overrides):
    data = {
        "branch": {"oid": "a" * 40, "head": "main", "upstream": "origin/main", "ab": "+0 -0"},
        "upstream_divergence": {"ahead": 0, "behind": 0},
        "changed": [], "conflicted": [], "untracked": [],
        "operation": None, "operation_commands": None, "bisecting": False,
    }
    data.update(overrides)
    return data


# ---- the explanation is only what git status reported ----------------------------

def test_a_clean_tracked_branch_needs_nothing():
    text = explain_repository_state(_status())
    assert "On branch main." in text
    assert "0 commits ahead of and 0 behind origin/main, as of the last fetch." in text
    assert "Nothing needs attention" in text
    assert "Next steps" not in text


def test_a_stopped_merge_names_conflicts_both_ways_out_and_what_abort_loses():
    text = explain_repository_state(_status(
        operation="merge",
        operation_commands={"continue": "git merge --continue", "abort": "git merge --abort"},
        conflicted=[{"path": "notes.txt", "state": "both modified"}],
    ))
    assert "A merge has stopped part-way" in text
    assert "Conflicted: notes.txt (both modified)." in text
    assert "`git add <path>` each one, then `git merge --continue`" in text
    assert "`git merge --abort`" in text and "discards any conflict resolution" in text
    assert "uncommitted changes when the merge started" in text


def test_a_rebase_with_no_conflicts_left_only_needs_continue_and_detached_is_not_scolded():
    text = explain_repository_state(_status(
        branch={"oid": "b" * 40, "head": "(detached)"}, upstream_divergence=None,
        operation="rebase",
        operation_commands={"continue": "git rebase --continue", "abort": "git rebase --abort"},
    ))
    assert "No conflicted files remain: `git rebase --continue`" in text
    assert "uncommitted changes when the merge started" not in text
    # HEAD is detached during a rebase by design; the keep-your-commits advice is not for it.
    assert "git switch -c" not in text


def test_a_detached_head_warns_where_its_commits_go():
    text = explain_repository_state(_status(branch={"oid": "c" * 40, "head": "(detached)"},
                                            upstream_divergence=None))
    assert f"HEAD is detached at {'c' * 12}" in text
    assert "`git switch -c <new-branch>`" in text and "reflog" in text
    assert "upstream" not in text.lower()


def test_no_upstream_is_unknown_divergence_not_zero():
    text = explain_repository_state(_status(branch={"oid": "a" * 40, "head": "feature"},
                                            upstream_divergence=None))
    assert "No upstream is configured for feature" in text and "unknown" in text
    assert "`git branch --set-upstream-to=<remote>/feature`" in text
    assert "`git push -u <remote> feature`" in text


def test_an_upstream_git_has_no_copy_of_is_reported_as_unknown():
    text = explain_repository_state(_status(branch={"oid": "a" * 40, "head": "feature",
                                                    "upstream": "origin/feature"},
                                            upstream_divergence=None))
    assert "origin/feature is configured but git has no copy of it" in text
    assert "`git branch --unset-upstream`" in text


@pytest.mark.parametrize(("ahead", "behind", "advice"), [
    (2, 3, "have diverged"), (0, 4, "`git pull` brings in the 4 commits"),
    (1, 0, "`git push` publishes the 1 local commit"),
])
def test_divergence_advice_follows_the_counts(ahead, behind, advice):
    text = explain_repository_state(_status(upstream_divergence={"ahead": ahead, "behind": behind}))
    assert advice in text


def test_partially_staged_files_say_what_a_commit_would_take():
    text = explain_repository_state(_status(
        changed=[{"path": "a", "staged": "M", "worktree": "M"},
                 {"path": "b", "staged": "A", "worktree": ""},
                 {"path": "c", "staged": "", "worktree": "M"}],
        untracked=["d"],
    ))
    assert "1 file staged only, 1 unstaged only, 1 with both staged and unstaged changes, 1 untracked." in text
    assert "`git commit` records only the staged part" in text


def test_during_an_operation_staged_work_is_flagged_for_the_continue_commit():
    text = explain_repository_state(_status(
        operation="merge",
        operation_commands={"continue": "git merge --continue", "abort": "git merge --abort"},
        changed=[{"path": "unrelated.txt", "staged": "A", "worktree": "M"}],
    ))
    assert "including unrelated staged changes, goes into the commit it creates" in text
    assert "throws away changes you have staged since the merge started" in text
    # Outside an operation `git commit` takes only the staged part; during one, continue
    # commits the index, so that advice would mislead.
    assert "`git commit` records only the staged part" not in text


def test_bisect_has_its_own_way_out():
    assert "`git bisect reset`" in explain_repository_state(_status(bisecting=True))


def test_the_advice_never_runs_anything_and_says_so():
    text = explain_repository_state(_status(operation="merge", operation_commands={
        "continue": "git merge --continue", "abort": "git merge --abort"}))
    assert "nothing was changed" in text
    assert "for you to run (Local Code Agent will not run them)" in text


# ---- routing ------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what state is my repo in?", "What's wrong with my repo?", "what is wrong with this repository",
    "what git operation is in progress?", "is a rebase in progress?", "Is a bisect in progress",
])
def test_state_questions_route_to_the_controller(text):
    decision = decide_route(text, active_repo_count=1)
    assert decision.action == RouteAction.WORK
    assert decision.rule_id == RULE_REPOSITORY_STATE
    assert decision.skill == REPOSITORY_STATE


@pytest.mark.parametrize("text", [
    '"what\'s wrong with my repo?"', "what's wrong with my repo? fix it",
    "what's wrong with my repo and abort the merge", "is a rebase in progress? abort it",
])
def test_quoted_or_compound_state_questions_do_not_run(text):
    assert decide_route(text, active_repo_count=1).action == RouteAction.MODEL_FALLBACK


def test_conflict_hunk_questions_stay_with_the_git_review_worker():
    decision = decide_route("explain this conflict", active_repo_count=1)
    assert (decision.rule_id, decision.skill) == (RULE_GIT_REVIEW, "git-review")


def test_admission_identity_is_bound_to_the_plan_and_procedure_bytes():
    first, second = repository_state_sha256("1" * 64), repository_state_sha256("2" * 64)
    assert first != second and len(first) == 64


# ---- the whole session path, on a real repository in trouble -----------------------

class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the repository state answer reached a model")


def _no_worker():
    raise AssertionError("the repository state answer started a model worker")


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],  # noqa: S607
        cwd=root, check=check, capture_output=True, text=True,
    )


def _snapshot(root):
    """Everything the explanation must leave exactly as it found it."""
    git_dir = root / ".git"
    return {
        "head": _git(root, "rev-parse", "HEAD").stdout,
        "refs": _git(root, "for-each-ref").stdout,
        "index": _git(root, "ls-files", "--stage").stdout,
        "status": _git(root, "status", "--porcelain=v2", "--branch").stdout,
        "merge_head": (git_dir / "MERGE_HEAD").read_text(encoding="utf-8"),
        "files": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in root.rglob("*") if path.is_file() and ".git" not in path.parts},
    }


def _stopped_merge_with_staged_and_unstaged_work(root):
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    (root / "notes.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-qm", "notes")
    _git(root, "switch", "-qc", "other")
    (root / "notes.txt").write_text("theirs\n", encoding="utf-8")
    _git(root, "commit", "-qam", "theirs")
    _git(root, "switch", "-q", base)
    (root / "notes.txt").write_text("ours\n", encoding="utf-8")
    _git(root, "commit", "-qam", "ours")
    assert _git(root, "merge", "other", check=False).returncode != 0, "the merge must stop"
    # Unrelated user work made while the merge is stopped: one file staged and then
    # changed again, one untracked.
    (root / "staged.txt").write_text("first\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "staged.txt").write_text("first\nsecond\n", encoding="utf-8")
    (root / "scratch.txt").write_text("mine\n", encoding="utf-8")
    return base


def test_whats_wrong_with_my_repo_explains_a_stopped_merge_and_changes_nothing(sandbox, tmp_path):
    root = sandbox.root
    _stopped_merge_with_staged_and_unstaged_work(root)
    before = _snapshot(root)

    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(root), _no_worker, events, allow_execution=False,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
    gateway = ConversationGateway(
        NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    try:
        answer = gateway.turn("what's wrong with my repo?")
        admitted = [e["payload"] for e in service.store.replay(service.stream_id, limit=1000)
                    if e["kind"] == "task.admitted"]
    finally:
        service.close()

    assert _snapshot(root) == before, "explaining the repository changed it"
    assert "A merge has stopped part-way" in answer, answer
    assert "Conflicted: notes.txt (both modified)." in answer
    assert "`git merge --continue`" in answer and "`git merge --abort`" in answer
    assert "1 with both staged and unstaged changes" in answer and "1 untracked" in answer
    assert "goes into the commit it creates" in answer
    (admission,) = admitted
    assert admission["origin"]["rule_id"] == RULE_REPOSITORY_STATE
    assert admission["skill"] == REPOSITORY_STATE


def test_the_abort_warning_matches_what_git_does_to_staged_work(tmp_path):
    """The advice must be true: a merge abort discards changes staged since it started."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "notes.txt").write_text("base\n", encoding="utf-8")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(root, "switch", "-qc", "other")
    (root / "notes.txt").write_text("theirs\n", encoding="utf-8")
    _git(root, "commit", "-qam", "theirs")
    _git(root, "switch", "-q", base)
    (root / "notes.txt").write_text("ours\n", encoding="utf-8")
    _git(root, "commit", "-qam", "ours")
    assert _git(root, "merge", "other", check=False).returncode != 0
    (root / "added.txt").write_text("new\n", encoding="utf-8")
    (root / "keep.txt").write_text("changed\n", encoding="utf-8")
    _git(root, "add", "added.txt", "keep.txt")
    assert _git(root, "merge", "--abort", check=False).returncode == 0
    assert not (root / "added.txt").exists()
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep\n"
