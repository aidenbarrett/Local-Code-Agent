"""The live activity pane is a compact card, not a second copy of the conversation.

Above the detail the card already states the request, result, evidence and next
action. Below it the controller's verdict lines stay verbatim but are not restated,
and a retained answer that the conversation already shows beneath its admitting turn
is not repeated. A task with no admitting turn in view keeps its retained answer
here, because this pane is then the only place it appears.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

from textual.widgets import Static

from local_agent.config import load_repo_config
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.task_read_model import OriginTurn, TaskSnapshot
from local_agent.session.textual_conversation import project_conversation
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import ConversationEntry, HubViewState
from local_agent.session.textual_live_app import LiveSessionHubApp, render_live_activity
from local_agent.session.workspaces import GitWorkspaceManager

TURN = OriginTurn("conv-1", 0, "a" * 64)


def _task(turn: OriginTurn | None) -> TaskSnapshot:
    task = TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=1, last_sequence=3, state="failed",
        execution_epoch=0, origin_kind="user_rule", repository_id="repo-1", skill="run-build",
        deadline_utc="2030-01-01T00:00:00Z", verdict="FAILED",
        verdict_reason="verification_failed", verdict_scope="observed_build_failure",
        closed_sequence=3, result_answer="first error at src/a.cpp:13", origin_turn=turn,
        requested_outcome="User request:\nbuild it\n\nDeterministic route (controller-owned provenance):",
    )
    task.verdict_lines = ("FAILED: fail.", "Reason: verification_failed.")
    return task


def test_an_answer_shown_under_its_turn_is_not_repeated_in_the_card():
    task = _task(TURN)
    state = HubViewState(conversation=(ConversationEntry("user", "build it", TURN),), tasks=(task,))
    card = render_live_activity(state)
    assert "src/a.cpp:13" not in card and "Retained result" not in card
    assert card.startswith("Requested outcome: build it\nRepository: repo-1\nResult: FAILED")
    assert "Deterministic route" not in card
    assert f"Task: {task.task_id}\nrun-build · failed · user_rule · tool none\n" in card
    assert "Controller verdict:\n  FAILED: fail.\n  Reason: verification_failed." in card
    assert "Verdict: FAILED" not in card


def test_an_answer_with_no_admitting_turn_in_view_stays_in_the_card():
    for turn, entries in ((None, (ConversationEntry("user", "build it", TURN),)),
                          (TURN, (ConversationEntry("user", "build it",
                                                    OriginTurn("conv-1", 0, "b" * 64)),)),
                          (TURN, ())):
        card = render_live_activity(HubViewState(conversation=entries, tasks=(_task(turn),)))
        assert "Retained result (unverified as completion):\nfirst error at src/a.cpp:13" in card
        assert "Requested outcome: User request: build it Deterministic route" in card


class _NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("this journey must not reach a model")


def _no_worker():
    raise AssertionError("this journey must not start a worker")


def test_a_real_failed_build_appears_once_on_screen(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), _no_worker, events, allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    gateway = ConversationGateway(
        _NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(CancellableDurableTaskExecutor(
            service, AdmittedDurableTaskController(service, controller))),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    try:
        gateway.turn("build it")
        feed = DurableHubFeed(service,
                              conversation_provider=lambda: project_conversation(gateway.session))

        async def scenario():
            app = LiveSessionHubApp(feed)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                conversation = str(app.query_one("#conversation", Static).render())
                card = str(app.query_one("#activity", Static).render())
                assert "FAILED (compile)" in conversation
                assert "FAILED (compile)" not in card
                assert card.startswith("Requested outcome: build it\n")
                assert "Result: FAILED" in card and "Tree SHA-256:" in card
        asyncio.run(scenario())
    finally:
        service.close()
