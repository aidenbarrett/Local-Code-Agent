from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.attention import render_attention
from local_agent.session.candidate_facts import CandidateFacts, validate_candidate
from local_agent.session.result_presentation import render_result_next_action
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, render_activity


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


def test_verified_prepared_candidate_has_one_review_and_apply_path():
    task = _task(verdict="VERIFIED")
    candidate_id = str(uuid4())
    assert candidate_id != task.task_id
    task.result_verified_at_completion = True
    task.candidate = _candidate("prepared", candidate_id, retained=True)

    rendered = render_activity(HubViewState(tasks=(task,)))
    assert rendered.count(f"/diff {candidate_id}") == 1
    assert rendered.count(f"/apply {candidate_id}") == 1
    assert task.task_id not in rendered.split("Candidate: READY", 1)[1]
    assert render_result_next_action(task) == (
        f"Next action: review the retained candidate with /diff {candidate_id}."
    )
    attention = render_attention((task,))
    assert attention.count(f"/diff {candidate_id}") == 1
    assert attention.count(f"/apply {candidate_id}") == 1
    assert task.task_id not in attention


def test_unverified_prepared_candidate_exposes_no_checkout_command():
    task = _task(verdict="FAILED")
    task.result_verified_at_completion = False
    task.candidate = _candidate("prepared", str(uuid4()), retained=True)

    rendered = render_activity(HubViewState(tasks=(task,)))
    assert "completion verification is not established" in rendered
    for command in ("/diff ", "/apply ", "/undo ", "/commit "):
        assert command not in rendered
    assert render_attention((task,)) == ""


def test_applied_candidate_exposes_only_undo_and_exact_commit():
    task = _task()
    candidate_id = str(uuid4())
    task.candidate = _candidate("applied", candidate_id)

    rendered = render_activity(HubViewState(tasks=(task,)))
    assert rendered.count(f"/undo {candidate_id}") == 1
    assert rendered.count(f"/commit {candidate_id}") == 1
    assert "/apply " not in rendered
    assert "/diff " not in rendered


@pytest.mark.parametrize("role", ["apply_refused", "undone", "discarded"])
def test_terminal_candidate_states_advertise_no_candidate_command(role):
    task = _task()
    task.candidate = _candidate(role, str(uuid4()))

    rendered = render_activity(HubViewState(tasks=(task,)))
    for command in ("/diff ", "/apply ", "/undo ", "/commit "):
        assert command not in rendered


def test_committed_candidate_names_commit_without_another_action():
    task = _task()
    commit = "c" * 40
    task.candidate = _candidate("committed", str(uuid4()), commit=commit)

    rendered = render_activity(HubViewState(tasks=(task,)))
    assert f"Candidate: COMMITTED · {commit}" in rendered
    for command in ("/diff ", "/apply ", "/undo ", "/commit "):
        assert command not in rendered
