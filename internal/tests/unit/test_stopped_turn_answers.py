"""The turn whose task was stopped is answered, not raised out of the conversation.

Stop revokes the execution epoch; the late worker result may not commit, and the
executor commits an explicit NO_VERDICT / cancel_unreconciled terminal instead. The
user who typed the stopped request must get that terminal as the answer. Build, git,
the durable store and the executor are real; the model blocks until Stop lands.
"""
from __future__ import annotations

import threading
from uuid import uuid4

from local_agent.config import load_repo_config
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager


class NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("the conversation model is not needed here")


def test_the_stopped_fix_turn_returns_its_durable_terminal(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    in_model = threading.Event()
    release = threading.Event()

    class BlocksUntilStopped:
        def chat(self, messages, tools=None, max_tokens=None):
            in_model.set()
            assert release.wait(30)
            return ChatResponse(content="late prose after Stop")

    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), BlocksUntilStopped, events, allow_execution=True,
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
        gateway.turn("build it")
        outcome: dict[str, object] = {}

        def fix() -> None:
            try:
                outcome["answer"] = gateway.turn("fix it")
            except BaseException as exc:  # noqa: BLE001 - the defect under test
                outcome["error"] = exc

        turn = threading.Thread(target=fix, daemon=True)
        turn.start()
        assert in_model.wait(30)
        fix_task = [e for e in service.store.replay(service.stream_id, limit=1000)
                    if e["kind"] == "task.admitted"][-1]["task_id"]
        executor.request_cancel(fix_task, execution_epoch=0)
        release.set()
        turn.join(60)

        assert "error" not in outcome, outcome.get("error")
        assert "Stop requested" in str(outcome["answer"])
        stopped = gateway.last_result
        assert stopped.task_id == fix_task
        assert (stopped.outcome, stopped.reason_code) == (TaskOutcome.NO_VERDICT, "cancel_unreconciled")
        assert stopped.verified_at_completion is False
    finally:
        release.set()
        service.close()
