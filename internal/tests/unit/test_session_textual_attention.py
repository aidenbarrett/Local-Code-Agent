from __future__ import annotations

from uuid import uuid4

from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, render_attention


def _task() -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=1,
        last_sequence=1,
        state="completed",
        execution_epoch=0,
        origin_kind="user_direct",
        repository_id="repo-1",
        skill="inspect",
        deadline_utc="2030-01-01T00:00:00Z",
    )


def test_attention_is_empty_when_no_durable_fact_needs_intervention():
    task = _task()
    task.verdict = "VERIFIED"
    assert render_attention(HubViewState(tasks=(task,))) == "ATTENTION\nNothing needs attention."


def test_attention_surfaces_failed_and_unknown_tasks():
    failed = _task()
    failed.verdict = "FAILED"
    failed.verdict_reason = "verification_failed"
    unknown = _task()
    unknown.verdict = "NO_VERDICT"
    unknown.verdict_reason = "cancel_unreconciled"

    rendered = render_attention(HubViewState(tasks=(failed, unknown)))

    assert f"{unknown.task_id} · NEEDS REVIEW · cancel_unreconciled" in rendered
    assert f"{failed.task_id} · FAILED · verification did not establish success" in rendered


def test_attention_prefers_durable_fault_over_terminal_verdict():
    task = _task()
    task.verdict = "NO_VERDICT"
    task.verdict_reason = "internal_error"
    task.faults.append(("runtime_unavailable", "runtime did not answer"))

    rendered = render_attention(HubViewState(tasks=(task,)))

    assert f"{task.task_id} · FAULT · runtime_unavailable · runtime did not answer" in rendered
    assert "NEEDS REVIEW" not in rendered
