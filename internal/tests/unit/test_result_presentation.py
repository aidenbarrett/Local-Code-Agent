from __future__ import annotations

from uuid import uuid4

from local_agent.session.result_presentation import render_result_evidence, render_result_summary
from local_agent.session.task_read_model import TaskSnapshot
from local_agent.session.textual_hub import HubViewState
from local_agent.session.textual_live_app import render_live_activity


def _task(*, verdict=None, reason=None, scope=None) -> TaskSnapshot:
    task = TaskSnapshot(
        task_id=str(uuid4()),
        admitted_sequence=1,
        last_sequence=1,
        state="completed",
        execution_epoch=0,
        origin_kind="user_direct",
        repository_id="repo-1",
        skill="build-and-test",
        deadline_utc="2030-01-01T00:00:00Z",
    )
    task.verdict = verdict
    task.verdict_reason = reason
    task.verdict_scope = scope
    return task


def test_verified_result_names_proven_scope_without_worker_prose():
    task = _task(
        verdict="VERIFIED",
        reason="verification_passed",
        scope="full_test; request_sha256=" + "a" * 64,
    )
    assert render_result_summary(task) == "Result: VERIFIED — full current-tree test proof."


def test_targeted_evidence_is_not_promoted_to_whole_task_verification():
    task = _task(
        verdict="FAILED",
        reason="verification_failed",
        scope="targeted_test; request_sha256=" + "b" * 64,
    )
    summary = render_result_summary(task)
    assert summary.startswith("Result: FAILED")
    assert "targeted test evidence does not certify the whole request" in summary
    assert "VERIFIED" not in summary


def test_cleanup_uncertainty_stays_no_verdict():
    task = _task(verdict="NO_VERDICT", reason="cancel_unreconciled", scope="none")
    assert render_result_summary(task) == (
        "Result: NO VERDICT — final outcome is unknown because cleanup is not fully reconciled."
    )


def test_refusal_explains_reason_without_claiming_execution():
    task = _task(verdict="REFUSED", reason="policy_denied", scope="none")
    assert render_result_summary(task) == (
        "Result: REFUSED — the controller did not authorize or execute the requested work "
        "(policy denied)."
    )


def test_pending_and_unknown_values_fail_closed():
    assert render_result_summary(_task()) == "Result: IN PROGRESS — no durable verdict yet."
    assert render_result_summary(_task(verdict="FUTURE_VALUE")) == (
        "Result: UNKNOWN — unsupported durable verdict 'FUTURE_VALUE'."
    )


def test_live_activity_puts_human_result_above_raw_controller_detail():
    task = _task(
        verdict="VERIFIED",
        reason="verification_passed",
        scope="full_build; request_sha256=" + "c" * 64,
    )
    task.verdict_lines = (
        "VERIFIED: pass.",
        "Reason: verification_passed.",
        "Proof scope: full_build.",
    )
    rendered = render_live_activity(HubViewState(tasks=(task,)))
    assert rendered.startswith("Result: VERIFIED — full current-tree build proof.\nEvidence:\n")
    assert "Verdict: VERIFIED · verification_passed" in rendered
    assert "Proof scope: full_build." in rendered


def test_live_activity_keeps_no_task_empty_state_without_fake_result():
    rendered = render_live_activity(HubViewState())
    assert "Result:" not in rendered
    assert "Task: none" in rendered


def test_result_evidence_names_durable_identity_scope_and_evidence_without_prose():
    task = _task(
        verdict="VERIFIED",
        reason="verification_passed",
        scope="full_test; request_sha256=" + "d" * 64,
    )
    task.execution_epoch = 3
    task.evidence_ids = ("ev-build", "ev-tests")
    task.result_verification_ran = True
    task.result_verified_at_completion = True

    rendered = render_result_evidence(task)
    assert "Repository: repo-1" in rendered
    assert "Execution epoch: 3" in rendered
    assert "Proof scope: full current-tree test proof" in rendered
    assert "Evidence IDs: ev-build, ev-tests" in rendered
    assert "Verification: passed at task completion" in rendered


def test_result_evidence_fails_closed_when_proof_is_not_established():
    task = _task(verdict="NO_VERDICT", reason="cleanup_unknown", scope=None)
    rendered = render_result_evidence(task)
    assert "Proof scope: not established" in rendered
    assert "Evidence IDs: none" in rendered
    assert "Verification: not established" in rendered
    assert "passed" not in rendered


def test_live_activity_keeps_evidence_separate_from_retained_worker_answer():
    task = _task(
        verdict="VERIFIED",
        reason="verification_passed",
        scope="full_build; request_sha256=" + "e" * 64,
    )
    task.evidence_ids = ("ev-1",)
    task.result_answer = "Worker prose that is not verification authority."
    task.result_verification_ran = True
    task.result_verified_at_completion = True

    rendered = render_live_activity(HubViewState(tasks=(task,)))
    assert rendered.index("Evidence:") < rendered.index("Retained result:")
    assert "Evidence IDs: ev-1" in rendered
    assert "Worker prose that is not verification authority." in rendered
