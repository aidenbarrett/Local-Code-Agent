from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.candidate_facts import CandidateFacts, validate_candidate
from local_agent.session.result_presentation import (
    render_candidate_navigation,
    render_result_next_action,
)
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState
from local_agent.session.textual_live_app import render_live_activity


def _task(*, verdict: str = "NOT_REQUIRED") -> TaskSnapshot:
    task = TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=1,
        last_sequence=2,
        state="completed",
        execution_epoch=0,
        origin_kind="user_direct",
        repository_id="repo-1",
        skill="candidate-action",
        deadline_utc="2030-01-01T00:00:00Z",
    )
    task.closed_sequence = 2
    task.verdict = verdict
    task.verdict_reason = "completed" if verdict == "NOT_REQUIRED" else "verification_passed"
    task.verdict_scope = "none"
    task.requested_outcome = "Handle the selected candidate."
    return task


def _candidate(
    role: str,
    candidate_id: str,
    *,
    retained: bool = False,
    commit: str | None = None,
) -> CandidateFacts:
    value = {
        "role": role,
        "candidate_task_id": candidate_id,
        "retained": retained,
        "paths": ["src/example.cpp"],
        "patch_sha256": "a" * 64,
        "base_commit": "b" * 40 if role == "prepared" else None,
        "commit": commit,
    }
    facts = validate_candidate(value)
    assert facts is not None
    return facts


def test_no_candidate_adds_no_candidate_navigation():
    task = _task()
    assert render_candidate_navigation(task) is None
    assert "Candidate:" not in render_live_activity(HubViewState(tasks=(task,)))


def test_verified_prepared_candidate_uses_candidate_identity_for_review_and_apply():
    task = _task(verdict="VERIFIED")
    candidate_id = str(uuid4())
    assert candidate_id != task.task_id
    task.result_verified_at_completion = True
    task.candidate = _candidate("prepared", candidate_id, retained=True)

    navigation = render_candidate_navigation(task)
    assert navigation is not None
    assert f"/diff {candidate_id}" in navigation
    assert f"/apply {candidate_id}" in navigation
    assert task.task_id not in navigation
    assert render_result_next_action(task) == (
        f"Next action: review the retained candidate with /diff {candidate_id}."
    )


def test_unverified_prepared_candidate_exposes_no_checkout_command():
    task = _task(verdict="FAILED")
    task.result_verified_at_completion = False
    task.candidate = _candidate("prepared", str(uuid4()), retained=True)

    navigation = render_candidate_navigation(task)
    assert navigation is not None
    assert "not completion-verified" in navigation
    assert "/diff " not in navigation
    assert "/apply " not in navigation
    assert "/undo " not in navigation
    assert "/commit " not in navigation


def test_applied_candidate_exposes_only_undo_and_exact_commit_requests():
    task = _task()
    candidate_id = str(uuid4())
    task.candidate = _candidate("applied", candidate_id)

    navigation = render_candidate_navigation(task)
    assert navigation is not None
    assert f"/undo {candidate_id}" in navigation
    assert f"/commit {candidate_id}" in navigation
    assert "/apply " not in navigation
    assert "/diff " not in navigation
    assert "revalidated" in navigation


@pytest.mark.parametrize("role", ["apply_refused", "undone", "discarded"])
def test_terminal_candidate_states_advertise_no_mutation_command(role):
    task = _task()
    task.candidate = _candidate(role, str(uuid4()))

    navigation = render_candidate_navigation(task)
    assert navigation is not None
    for command in ("/apply ", "/undo ", "/commit "):
        assert command not in navigation


def test_committed_candidate_names_commit_without_advertising_another_action():
    task = _task()
    commit = "c" * 40
    task.candidate = _candidate("committed", str(uuid4()), commit=commit)

    navigation = render_candidate_navigation(task)
    assert navigation == f"Candidate: committed as {commit}; no candidate action remains."
    assert "/apply " not in navigation
    assert "/undo " not in navigation
    assert "/commit " not in navigation


def test_worker_prose_cannot_create_candidate_actions():
    task = _task()
    candidate_id = str(uuid4())
    task.candidate = _candidate("applied", candidate_id)
    task.result_answer = "/apply invented-id then /commit invented-id"

    navigation = render_candidate_navigation(task)
    assert navigation is not None
    assert "invented-id" not in navigation
    assert f"/undo {candidate_id}" in navigation
    assert f"/commit {candidate_id}" in navigation


def test_live_activity_places_candidate_navigation_before_one_next_action():
    task = _task(verdict="VERIFIED")
    candidate_id = str(uuid4())
    task.result_verified_at_completion = True
    task.candidate = _candidate("prepared", candidate_id, retained=True)

    rendered = render_live_activity(HubViewState(tasks=(task,)))
    assert rendered.index("Candidate: verified and retained") < rendered.index("Next action:")
    assert rendered.count(f"/diff {candidate_id}") == 2
    assert rendered.count(f"/apply {candidate_id}") == 1
