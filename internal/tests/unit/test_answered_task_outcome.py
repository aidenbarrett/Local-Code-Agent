"""Read-only answers complete honestly without becoming engineering passes."""
from __future__ import annotations

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import TaskOutcome, TaskVerdict
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.task_controller import TaskController


def _run_navigation(sandbox, evidence_ids: list[str]):
    turns = [
        ChatResponse(tool_calls=[tool_call(
            "read_file", {"path": "include/sandbox/ring_buffer.hpp"}, "read",
        )]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis",
            "summary": "RingBuffer is declared in include/sandbox/ring_buffer.hpp.",
            "evidence_ids": evidence_ids,
        }, "answer")]),
    ]
    controller = TaskController(
        load_repo_config(sandbox.root),
        lambda: ScriptedClient(turns),
        EventBuffer("answered-test"),
    )
    return controller.run("Where is RingBuffer?", skill_name="repo-navigation")


def test_grounded_read_only_answer_has_its_own_completed_outcome(sandbox):
    result = _run_navigation(sandbox, ["read_file:0"])

    assert result.outcome is TaskOutcome.ANSWERED
    assert result.projection.verdict is TaskVerdict.NOT_REQUIRED
    assert result.projection.terminal_state.value == "completed"
    assert result.reason_code == "verification_not_required"
    assert result.verified_at_completion is False
    assert result.outcome.succeeded is False
    assert "Read-only answer (not a verified engineering outcome):" in result.render()


def test_uncited_read_only_prose_remains_no_verdict(sandbox):
    result = _run_navigation(sandbox, [])

    assert result.outcome is TaskOutcome.NO_VERDICT
    assert result.reason_code == "missing_evidence"


def test_unknown_citation_remains_no_verdict(sandbox):
    result = _run_navigation(sandbox, ["read_file:99"])

    assert result.outcome is TaskOutcome.NO_VERDICT
    assert result.reason_code == "missing_evidence"
