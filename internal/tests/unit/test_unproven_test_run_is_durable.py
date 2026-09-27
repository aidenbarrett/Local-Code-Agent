"""A test run that executed but proves nothing is recorded, and does not jam the task.

Found on the Panther Lake run: `fix it` after a failing test ran `run_test` in the
fresh candidate workspace, where nothing had been built. The tool reports execution
OK, domain UNKNOWN, reason no_build_record. The durable contract refused any reason
on an OK execution, so the finish raised, the call stayed open, and every later
tool call in the task failed with "a previous tool call is still open".
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_activity import DurableActivityError, durable_tool_reason
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.task_read_model import project_task
from local_agent.session.workspaces import GitWorkspaceManager


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the conversation model is not needed here")


@pytest.mark.parametrize(("reason", "code"), [("stale_binary", "stale_evidence"),
                                               ("no_build_record", "missing_evidence"),
                                               ("profile_mismatch", "scope_changed")])
def test_an_unproven_clean_run_keeps_its_reason(reason, code):
    assert durable_tool_reason(execution="ok", reason=reason, domain="unknown") == code


@pytest.mark.parametrize(("reason", "domain"), [("stale_binary", "pass"), ("stale_binary", "fail"),
                                                 ("policy_denied", "unknown")])
def test_any_other_reason_on_a_clean_run_is_refused(reason, domain):
    with pytest.raises(DurableActivityError):
        durable_tool_reason(execution="ok", reason=reason, domain=domain)


def test_fix_it_that_tests_before_building_records_every_call_and_finishes(sandbox, tmp_path):
    sandbox.scenario("test_failure")
    worker_turns = [
        ChatResponse(tool_calls=[tool_call("run_test", {}, "t1")]),
        ChatResponse(tool_calls=[tool_call("run_test", {}, "t2")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "diagnosis", "summary": "tests were never built here", "evidence_ids": []}, "s")]),
    ]
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), lambda: ScriptedClient(list(worker_turns)), events,
        allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
    gateway = ConversationGateway(
        NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    try:
        gateway.turn("run the tests")
        gateway.turn("fix it")
        fixed = gateway.last_result
        stream = service.store.replay(service.stream_id, limit=1000)
        finishes = [e["payload"] for e in stream
                    if e["kind"] == "tool.finished" and e["task_id"] == fixed.task_id]
        assert [(f["tool_name"], f["execution"], f["domain"], f["reason_code"]) for f in finishes] == [
            ("run_test", "ok", "unknown", "missing_evidence"),
            ("run_test", "ok", "unknown", "missing_evidence"),
        ]
        task = project_task(stream, fixed.task_id)
        assert task.terminal is True and task.cleanup == "not_needed"
        assert fixed.verified_at_completion is False
    finally:
        service.close()
