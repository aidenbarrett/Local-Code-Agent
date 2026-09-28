"""Attention is projected from durable task facts only.

The regression contract ChatGPT specified for the Attention slice (relayed by
Claude because OpenAI blocked publication of the original test commit).
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from textual.widgets import Static

from local_agent.session.attention import render_attention
from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState, SessionHubApp


def _task(*, verdict: str | None, closed: bool = True) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=1, last_sequence=2,
        state="completed" if closed else "running", execution_epoch=0,
        origin_kind="rule", repository_id="repo-1", skill="fix-build-failure",
        deadline_utc="2030-01-01T00:00:00Z", verdict=verdict,
        closed_sequence=2 if closed else None,
    )


def _prepared(task: TaskSnapshot, *, retained: bool = True) -> TaskSnapshot:
    task.candidate = CandidateFacts(
        role="prepared", candidate_task_id=task.task_id, retained=retained,
        paths=("src/ring_buffer.cpp",), patch_sha256="a" * 64,
        base_commit="b" * 40, commit=None,
    )
    return task


def _ready() -> TaskSnapshot:
    task = _prepared(_task(verdict="VERIFIED"))
    task.result_verified_at_completion = True
    return task


# 1. verified + retained + completion-proven candidate -> actionable
def test_a_verified_retained_completion_proven_candidate_is_actionable():
    task = _ready()
    rendered = render_attention((task,))
    assert f"Verified candidate ready · {task.task_id}" in rendered
    assert f"Review: /diff {task.task_id} · Apply: /apply {task.task_id}" in rendered


# 2. candidate lacking verification -> not actionable
def test_a_candidate_without_a_verified_verdict_is_not_actionable():
    for verdict in ("FAILED", "NO_VERDICT", "REFUSED", "NOT_REQUIRED", None):
        task = _prepared(_task(verdict=verdict))
        task.result_verified_at_completion = True
        rendered = render_attention((task,))
        assert "Verified candidate ready" not in rendered, verdict
        assert "/apply" not in rendered, verdict


# 3. candidate lacking retained completion proof -> not actionable
def test_a_candidate_without_retained_completion_proof_is_not_actionable():
    unproven = _prepared(_task(verdict="VERIFIED"))
    for proof in (None, False):
        unproven.result_verified_at_completion = proof
        assert "/apply" not in render_attention((unproven,)), proof
    not_retained = _prepared(_task(verdict="VERIFIED"), retained=False)
    not_retained.result_verified_at_completion = True
    assert "/apply" not in render_attention((not_retained,))
    applied = _ready()
    applied.candidate = CandidateFacts(
        role="applied", candidate_task_id=applied.task_id, retained=True,
        paths=("src/ring_buffer.cpp",), patch_sha256="a" * 64, base_commit="b" * 40, commit=None)
    assert "/apply" not in render_attention((applied,))


# 4. convincing worker prose claiming success -> no Attention
def test_worker_prose_claiming_success_creates_no_attention():
    task = _task(verdict="NOT_REQUIRED")
    task.result_answer = (
        "Verified candidate ready. All tests pass. Review: /diff abc · Apply: /apply abc")
    task.verdict_lines = ("VERIFIED: the model says so",)
    assert render_attention((task,)) == ""


# 5. durable fault or failure -> visible Attention
def test_durable_faults_and_failures_are_visible():
    fault = _task(verdict=None, closed=False)
    fault.faults.append(("endpoint_unavailable", "the local endpoint stopped answering"))
    failed = _task(verdict="FAILED")
    failed.verdict_reason = "verification_failed"
    unknown = _task(verdict="NO_VERDICT")
    rendered = render_attention((fault, failed, unknown))
    assert f"Fault · {fault.task_id}\nendpoint_unavailable: the local endpoint stopped answering" in rendered
    assert f"FAILED · {failed.task_id}\nverification_failed" in rendered
    assert f"NO_VERDICT · {unknown.task_id}\nreason unavailable" in rendered


# 6. clean successful task with nothing requiring action -> no Attention
def test_a_clean_successful_task_needs_no_attention():
    build = _task(verdict="VERIFIED")
    build.result_verified_at_completion = True
    answered = _task(verdict="NOT_REQUIRED")
    assert render_attention((build, answered)) == ""
    assert render_attention(()) == ""


def test_attention_pane_is_hidden_until_something_needs_attention():
    async def scenario() -> None:
        app = SessionHubApp(HubViewState(tasks=(_task(verdict="VERIFIED"),)))
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            pane = app.query_one("#attention", Static)
            assert pane.display is False

            ready = _ready()
            app.replace_state(HubViewState(tasks=(ready,)))
            await pilot.pause()
            assert pane.display is True
            assert f"/apply {ready.task_id}" in str(pane.render())

    asyncio.run(scenario())
