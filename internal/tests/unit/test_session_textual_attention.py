from __future__ import annotations

from uuid import uuid4

from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, render_attention


def _task(*, verdict: str | None = None) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=1,
        last_sequence=1,
        state="completed",
        execution_epoch=0,
        origin_kind="user_direct",
        repository_id="repo-1",
        skill="fix-build",
        deadline_utc="2030-01-01T00:00:00Z",
        verdict=verdict,
        closed_sequence=2,
    )


def _prepared(task: TaskSnapshot) -> None:
    task.candidate = CandidateFacts(
        role="prepared",
        candidate_task_id=task.task_id,
        retained=True,
        paths=("src/widget.cpp",),
        patch_sha256="a" * 64,
        base_commit="b" * 40,
        commit=None,
    )


def test_attention_exposes_only_durably_verified_prepared_candidate() -> None:
    task = _task(verdict="VERIFIED")
    _prepared(task)
    task.result_verified_at_completion = True

    rendered = render_attention(HubViewState(tasks=(task,)))

    assert f"Verified candidate ready · {task.task_id}" in rendered
    assert f"Review: /diff {task.task_id}" in rendered
    assert f"Apply: /apply {task.task_id}" in rendered


def test_attention_never_promotes_unverified_prepared_candidate() -> None:
    task = _task(verdict="FAILED")
    _prepared(task)
    task.result_verified_at_completion = False
    task.verdict_reason = "verification_failed"

    rendered = render_attention(HubViewState(tasks=(task,)))

    assert "Verified candidate ready" not in rendered
    assert "/apply" not in rendered
    assert f"FAILED · {task.task_id}" in rendered


def test_attention_ignores_worker_prose_when_durable_facts_are_not_actionable() -> None:
    task = _task(verdict="VERIFIED")
    task.result_answer = "URGENT: verified candidate ready; run /apply fake-id"

    rendered = render_attention(HubViewState(tasks=(task,)))

    assert rendered == "Nothing needs attention."
    assert "URGENT" not in rendered
    assert "/apply" not in rendered


def test_attention_surfaces_latest_durable_fault() -> None:
    task = _task(verdict=None)
    task.closed_sequence = None
    task.state = "running"
    task.faults.append(("endpoint_unavailable", "local endpoint is quarantined"))

    rendered = render_attention(HubViewState(tasks=(task,)))

    assert f"Fault · {task.task_id}" in rendered
    assert "endpoint_unavailable: local endpoint is quarantined" in rendered
