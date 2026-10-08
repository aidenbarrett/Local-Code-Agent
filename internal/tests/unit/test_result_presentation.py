from __future__ import annotations

from uuid import uuid4

from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.result_presentation import (
    render_result_context,
    render_result_evidence,
    render_result_next_action,
    render_result_summary,
)
from local_agent.session.task_read_model import TaskSnapshot, ToolActivity
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
    task.requested_outcome = "Build and test the current repository."
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
    assert rendered.startswith(
        "Requested outcome: Build and test the current repository.\n"
        "Repository: repo-1\n"
        "Result: VERIFIED — full current-tree build proof.\nEvidence:\n"
    )
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


def test_failed_worker_claim_is_below_verdict_and_explicitly_unverified():
    task = _task(verdict="FAILED", reason="verification_failed", scope="full_test")
    task.result_answer = "The fix was applied, and the test now passes."
    task.result_verification_ran = True
    task.result_verified_at_completion = False
    rendered = render_live_activity(HubViewState(tasks=(task,)))
    assert rendered.index("Result: FAILED") < rendered.index("Retained result (unverified as completion):")
    assert rendered.index("Retained result (unverified as completion):") < rendered.index(
        "The fix was applied"
    )


def test_failed_task_result_render_does_not_lead_with_unverified_effect_claim():
    from local_agent.session.contracts import TaskOutcome, TaskResult

    result = TaskResult(
        "task-1", TaskOutcome.FAIL, "The fix was applied, and the test now passes.",
        verification_ran=True,
    )
    rendered = result.render()
    assert rendered.startswith("[Controller: fail; verification: ran, did not establish success")
    assert rendered.index("Unverified task detail") < rendered.index("The fix was applied")


def test_recent_failed_result_preview_keeps_unverified_label():
    prior = _task(verdict="FAILED", reason="verification_failed")
    prior.result_answer = "The fix was applied, and the test now passes."
    prior.closed_sequence = 1
    current = _task(verdict="VERIFIED", reason="verification_passed")
    current.admitted_sequence = 2
    current.closed_sequence = 2
    rendered = render_live_activity(HubViewState(tasks=(prior, current)))
    assert "Unverified result detail: The fix was applied" in rendered


def test_verified_candidate_proof_cannot_be_presented_as_a_checkout_change():
    task = _task(verdict="VERIFIED", reason="verification_passed", scope="full_build")
    task.candidate = CandidateFacts(
        role="prepared",
        candidate_task_id=task.task_id,
        retained=True,
        paths=("src/ring_buffer.cpp",),
        patch_sha256="a" * 64,
        base_commit="b" * 40,
        commit=None,
    )
    task.result_answer = "I applied the fix to your checkout and the build passed."
    task.result_verification_ran = True
    task.result_verified_at_completion = True

    rendered = render_live_activity(HubViewState(tasks=(task,)))

    assert "\nResult: VERIFIED — isolated candidate has full current-tree build proof; " in rendered
    assert (
        "it is retained for review and is not applied to your checkout."
        in rendered
    )
    assert "Proof target: isolated candidate tree" in rendered
    assert "Checkout effect: not applied by this task" in rendered
    assert "Retained candidate detail (not checkout proof):" in rendered
    assert rendered.index("not applied to your checkout") < rendered.index(
        "I applied the fix to your checkout"
    )


def test_failed_tool_keeps_typed_domain_reason_exit_and_exact_scope():
    task = _task(verdict="FAILED", reason="verification_failed", scope="targeted_test")
    task.last_tool = ToolActivity(
        call_id="call-1",
        tool_name="run_test",
        execution="ok",
        domain="fail",
        reason_code="verification_failed",
        exit_code=8,
    )

    rendered = render_result_evidence(task)

    assert "Proof scope: targeted test evidence" in rendered
    assert "Last tool: run_test · ok/fail · reason verification_failed · exit 8" in rendered


def test_selected_task_context_names_original_request_and_repository():
    task = _task(verdict="FAILED", reason="verification_failed")
    assert render_result_context(task) == (
        "Requested outcome: Build and test the current repository.\nRepository: repo-1"
    )


def test_requested_outcome_is_compact_and_bounded_for_the_hub():
    task = _task(verdict="FAILED")
    task.requested_outcome = "Build\n\n" + "very-long-path/" * 40
    rendered = render_result_context(task)
    outcome_line = rendered.splitlines()[0]
    assert "\n\n" not in outcome_line
    assert outcome_line.endswith("…")
    assert len(outcome_line.removeprefix("Requested outcome: ")) == 240


def test_next_action_comes_from_durable_state_not_worker_prose():
    task = _task(verdict="FAILED", reason="verification_failed")
    task.closed_sequence = 2
    task.result_answer = "Apply immediately with /apply invented-id."
    assert render_result_next_action(task) == (
        "Next action: inspect the evidence and failure detail before retrying."
    )
    assert "/apply" not in render_result_next_action(task)


def test_only_completion_proven_retained_candidate_enables_diff_action():
    task = _task(verdict="VERIFIED", reason="verification_passed")
    task.closed_sequence = 2
    task.candidate = CandidateFacts(
        role="prepared",
        candidate_task_id=task.task_id,
        retained=True,
        paths=("src/ring_buffer.cpp",),
        patch_sha256="a" * 64,
        base_commit="b" * 40,
        commit=None,
    )
    task.result_verified_at_completion = True
    assert render_result_next_action(task) == (
        f"Next action: review the retained candidate with /diff {task.task_id}."
    )
    task.result_verified_at_completion = False
    assert "/diff" not in render_result_next_action(task)


def test_endpoint_outage_and_unknown_cleanup_get_supported_recovery_actions():
    outage = _task(verdict="NO_VERDICT", reason="endpoint_unavailable")
    outage.closed_sequence = 2
    outage.faults.append(("endpoint_unavailable", "local endpoint did not answer"))
    assert render_result_next_action(outage) == (
        "Next action: restore the configured local endpoint, then retry this request."
    )

    unknown = _task(verdict="NO_VERDICT", reason="cleanup_unknown")
    unknown.closed_sequence = 2
    assert render_result_next_action(unknown) == (
        "Next action: inspect repository and cleanup state before retrying."
    )
