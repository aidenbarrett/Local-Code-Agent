from __future__ import annotations

from uuid import uuid4

from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, render_activity


def _terminal(*, sequence: int, state: str = "failed", verdict: str = "FAILED") -> TaskSnapshot:
    task = TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=sequence,
        last_sequence=sequence + 2,
        state=state,
        execution_epoch=0,
        origin_kind="user_direct",
        repository_id="repo-1",
        skill="task-diagnostic",
        deadline_utc="2026-09-22T16:00:00Z",
    )
    task.verdict = verdict
    task.verdict_reason = "fixture"
    task.closed_sequence = sequence + 2
    return task


def _running(*, sequence: int) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=sequence,
        last_sequence=sequence + 1,
        state="running",
        execution_epoch=0,
        origin_kind="rule",
        repository_id="repo-1",
        skill="build-and-test",
        deadline_utc="2026-09-22T16:00:00Z",
    )


def test_activity_exposes_full_recent_terminal_task_ids_for_copyable_followups():
    one = _terminal(sequence=1)
    two = _terminal(sequence=4, state="completed", verdict="VERIFIED")
    current = _running(sequence=7)

    rendered = render_activity(HubViewState(tasks=(one, two, current)))

    assert f"Task: {current.task_id}" in rendered
    assert "Recent tasks (copy full ID for follow-up):" in rendered
    assert f"{two.task_id} · completed · VERIFIED" in rendered
    assert f"{one.task_id} · failed · FAILED" in rendered
    # IDs are deliberately not shortened: deterministic explicit referents use the
    # canonical durable UUID rather than a fuzzy prefix.
    assert two.task_id in rendered
    assert one.task_id in rendered


def test_recent_task_history_is_bounded_to_three_and_excludes_current_card():
    older = [_terminal(sequence=1 + index * 3) for index in range(4)]
    current = _running(sequence=20)
    rendered = render_activity(HubViewState(tasks=tuple(older) + (current,)))

    assert older[3].task_id in rendered
    assert older[2].task_id in rendered
    assert older[1].task_id in rendered
    assert older[0].task_id not in rendered
    assert rendered.count(current.task_id) == 1


def test_when_latest_task_is_terminal_recent_list_contains_only_prior_tasks():
    prior = _terminal(sequence=1)
    latest = _terminal(sequence=4)
    rendered = render_activity(HubViewState(tasks=(prior, latest)))

    assert f"Task: {latest.task_id}" in rendered
    assert f"{prior.task_id} · failed · FAILED" in rendered
    assert rendered.count(latest.task_id) == 1
