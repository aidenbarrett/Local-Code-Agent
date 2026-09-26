from __future__ import annotations

from uuid import uuid4

from local_agent.session.result_presentation import render_result_summary
from local_agent.session.task_read_model import TaskSnapshot


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
