"""Product verification preserves two facts: whether a check ran and whether it proved success."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from local_agent.agent.state import AgentState
from local_agent.session.contracts import TaskOutcome, TaskResult


def test_field_order_is_pinned_because_callers_still_pass_positionally():
    assert [field.name for field in dataclasses.fields(TaskResult)] == [
        "task_id",
        "outcome",
        "answer",
        "verified_at_completion",
        "evidence_ids",
        "metrics",
        "verification_ran",
        "reason_code",
    ]


def test_positional_evidence_does_not_shift_into_verification_flag():
    result = TaskResult("t1", TaskOutcome.PASS, "Done.", True, ("a:0", "b:1"))
    assert result.evidence_ids == ("a:0", "b:1")
    assert result.verification_ran is True
    assert "evidence: 2 item(s)" in result.render()


def test_failed_check_is_distinct_from_no_check():
    ran = TaskResult("t", TaskOutcome.FAIL, "failed", False, verification_ran=True)
    never = TaskResult("t", TaskOutcome.FAIL, "stopped", False, verification_ran=False)
    assert ran.verification_ran is True
    assert never.verification_ran is False
    assert "ran, did not establish success" in ran.render()
    assert "not established" in never.render()


def test_verified_success_requires_verification_to_have_run():
    with pytest.raises(ValueError, match="requires verification_ran"):
        TaskResult("t", TaskOutcome.PASS, "claimed", True, verification_ran=False)


def test_success_still_requires_verified_completion():
    with pytest.raises(ValueError, match="must agree"):
        TaskResult("t", TaskOutcome.PASS, "unproved", False, verification_ran=True)


def test_verification_ran_is_inferred_when_unset():
    assert TaskResult("t", TaskOutcome.PASS, "ok", True).verification_ran is True
    assert TaskResult("t", TaskOutcome.FAIL, "bad", False).verification_ran is False


def test_agent_state_already_tracks_attempted_separately():
    fields = {field.name for field in dataclasses.fields(AgentState)}
    assert {"verified", "verification_attempted"} <= fields
    state = AgentState(task="t", repo_root=Path("."))
    state.verification_attempted = True
    assert state.verification_attempted is True
    assert state.verified is False
