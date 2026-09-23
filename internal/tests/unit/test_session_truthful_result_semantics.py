from __future__ import annotations

import pytest

from local_agent.session.contracts import TaskOutcome, TaskResult, TaskVerdict
from local_agent.session.results import VerdictReason, verdict_block_from_task_result


def test_endpoint_outage_is_not_rendered_as_policy_refusal():
    result = TaskResult(
        "task-1",
        TaskOutcome.BLOCKED,
        "Inference endpoint became unavailable.",
        False,
        verification_ran=False,
        reason_code="endpoint_unavailable",
    )

    block = verdict_block_from_task_result(result)

    assert block.verdict is TaskVerdict.REFUSED
    assert block.reason_code is VerdictReason.ENDPOINT_UNAVAILABLE
    assert "Reason: endpoint_unavailable." in block.rendered_lines
    assert "Reason: policy_denied." not in block.rendered_lines


def test_observed_verification_failure_remains_distinct_from_incomplete_verification():
    observed_failure = TaskResult(
        "task-observed",
        TaskOutcome.FAIL,
        "Tests ran and failed.",
        False,
        verification_ran=True,
        reason_code="verification_failed",
    )
    incomplete = TaskResult(
        "task-incomplete",
        TaskOutcome.FAIL,
        "No verifying evidence was produced.",
        False,
        verification_ran=False,
        reason_code="missing_evidence",
    )

    observed_block = verdict_block_from_task_result(observed_failure)
    incomplete_block = verdict_block_from_task_result(incomplete)

    assert observed_block.reason_code is VerdictReason.VERIFICATION_FAILED
    assert incomplete_block.reason_code is VerdictReason.MISSING_EVIDENCE
    assert observed_block.verdict is incomplete_block.verdict is TaskVerdict.FAILED


def test_reason_code_cannot_contradict_the_structured_verdict():
    result = TaskResult(
        "task-1",
        TaskOutcome.BLOCKED,
        "Blocked.",
        False,
        reason_code="verification_failed",
    )

    with pytest.raises(ValueError, match="incompatible"):
        verdict_block_from_task_result(result)


def test_controller_render_preserves_reason_without_changing_exit_outcome():
    result = TaskResult(
        "task-1",
        TaskOutcome.NO_VERDICT,
        "Controller stopped unexpectedly.",
        False,
        reason_code="controller_crash",
    )

    rendered = result.render()

    assert "Controller: no_verdict" in rendered
    assert "reason: controller_crash" in rendered
