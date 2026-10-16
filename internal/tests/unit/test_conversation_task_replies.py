"""A task's recorded result appears in the conversation under the turn that admitted it.

The conversation store keeps only the user's turn for task work; the result is a
durable task fact. The Hub projects that fact beneath the exact admitting turn
(conversation id, turn index and content hash all equal), so a viewer sees each
request answered in place. Nothing else may bind a result to a turn.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from textual.widgets import Static

from local_agent.config import load_repo_config
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import Turn, new_session, turn_ref
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.task_read_model import (
    OriginTurn,
    TaskReadModelError,
    TaskSnapshot,
    origin_turn,
)
from local_agent.session.textual_conversation import project_conversation
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import (
    ConversationEntry,
    render_conversation,
    render_task_reply,
)
from local_agent.session.textual_live_app import LiveSessionHubApp
from local_agent.session.workspaces import GitWorkspaceManager


def _turn(index: int, sha: str = "a" * 64, conversation: str = "conv-1") -> OriginTurn:
    return OriginTurn(conversation, index, sha)


def _task(turn: OriginTurn | None, *, sequence: int, answer: str = "recorded answer",
          verdict: str | None = "FAILED", closed: bool = True) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=sequence, last_sequence=sequence,
        state="completed" if closed else "running", execution_epoch=0,
        origin_kind="user_rule", repository_id="repo", skill="run-build",
        deadline_utc="2030-01-01T00:00:00Z", verdict=verdict if closed else None,
        verdict_reason="verification_failed" if closed else None,
        closed_sequence=sequence if closed else None, result_answer=answer,
        origin_turn=turn,
    )


def test_a_result_is_shown_under_exactly_its_admitting_turn():
    first, second = _turn(0), _turn(2, "b" * 64)
    entries = (
        ConversationEntry("user", "build it", first),
        ConversationEntry("user", "hello", _turn(1, "c" * 64)),
        ConversationEntry("assistant", "hi", _turn(1, "d" * 64)),
        ConversationEntry("user", "run the tests", second),
    )
    rendered = render_conversation(entries, (
        _task(second, sequence=9, answer="tests answer"),
        _task(first, sequence=3, answer="build answer"),
    ))
    assert rendered.index("build it") < rendered.index("build answer") < rendered.index("hello")
    assert rendered.index("run the tests") < rendered.index("tests answer")
    assert rendered.count("LCA · FAILED · verification failed") == 2


@pytest.mark.parametrize("other", [
    _turn(0, "f" * 64),                    # same index, edited/different turn content
    _turn(1),                              # same content hash, different index
    _turn(0, conversation="conv-2"),       # another conversation
    None,                                  # no admitting turn recorded
])
def test_a_result_never_binds_to_a_turn_it_was_not_admitted_from(other):
    entries = (ConversationEntry("user", "build it", _turn(0)),)
    rendered = render_conversation(entries, (_task(other, sequence=1, answer="stray"),))
    assert "stray" not in rendered and "LCA ·" not in rendered


def test_entries_without_a_turn_reference_render_exactly_as_before():
    entries = (ConversationEntry("user", "build it"), ConversationEntry("assistant", "ok"))
    assert render_conversation(entries, (_task(_turn(0), sequence=1),)) == "YOU\nbuild it\n\nLCA\nok"


def test_several_tasks_from_one_turn_appear_in_admission_order():
    turn = _turn(0)
    rendered = render_conversation(
        (ConversationEntry("user", "do both", turn),),
        (_task(turn, sequence=7, answer="second"), _task(turn, sequence=2, answer="first")),
    )
    assert rendered.index("first") < rendered.index("second")


def test_an_open_task_says_it_is_working_and_claims_no_verdict():
    reply = render_task_reply(_task(_turn(0), sequence=1, closed=False, answer="partial"))
    assert reply == "LCA · working · running"
    assert "partial" not in reply


def test_a_closed_task_without_retained_text_says_so():
    reply = render_task_reply(replace(_task(_turn(0), sequence=1), result_answer=None))
    assert reply.endswith("(recorded result text unavailable)")


def test_origin_turn_parses_the_admission_reference_and_refuses_malformed_ones():
    ref = {"conversation_id": "c", "turn_index": 4, "turn_sha256": "0" * 64}
    assert origin_turn({"kind": "user_rule", "turn_ref": ref}) == OriginTurn("c", 4, "0" * 64)
    assert origin_turn({"kind": "model_proposal"}) is None
    for bad in ({**ref, "turn_index": -1}, {**ref, "turn_index": True},
                {**ref, "turn_sha256": "A" * 64}, {**ref, "extra": 1},
                {"conversation_id": "c", "turn_index": 4}, "not-a-ref"):
        with pytest.raises(TaskReadModelError):
            origin_turn({"kind": "user_rule", "turn_ref": bad})


def test_projected_entries_carry_the_stored_turn_reference():
    session = new_session("p", "m", "d")
    session.turns.extend([Turn("user", "build it", "2026-10-17T00:00:00Z", 0),
                          Turn("assistant", "ok", "2026-10-17T00:00:01Z", 0)])
    for index, entry in enumerate(project_conversation(session)):
        ref = turn_ref(session, index)
        assert entry.turn == OriginTurn(str(ref["conversation_id"]), index,
                                        str(ref["turn_sha256"]))


class _NoModel:
    def chat(self, messages, tools=None, max_tokens=None):
        raise AssertionError("this journey must not reach a model")


def _no_worker():
    raise AssertionError("this journey must not start a worker")


def test_a_real_failed_build_is_answered_under_build_it_in_the_live_hub(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(sandbox.root), _no_worker, events, allow_execution=True,
        workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
    )
    executor = CancellableDurableTaskExecutor(
        service, AdmittedDurableTaskController(service, controller))
    gateway = ConversationGateway(
        _NoModel(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    try:
        gateway.turn("build it")
        recorded = gateway.last_result
        feed = DurableHubFeed(service,
                              conversation_provider=lambda: project_conversation(gateway.session))

        async def scenario():
            app = LiveSessionHubApp(feed)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                text = str(app.query_one("#conversation", Static).render())
                assert "YOU\nbuild it" in text
                reply = text.index("LCA · FAILED · verification failed")
                assert reply > text.index("build it")
                assert "FAILED (compile)" in text[reply:]
                assert recorded.task_id not in text, "the reply led with an identifier"
        asyncio.run(scenario())
    finally:
        service.close()


def _long_conversation(count: int) -> tuple[tuple[ConversationEntry, ...], tuple[TaskSnapshot, ...]]:
    entries = tuple(ConversationEntry("user", f"request {i}", _turn(i)) for i in range(count))
    tasks = tuple(_task(_turn(i), sequence=i + 1, answer=f"answer {i}\n" * 3) for i in range(count))
    return entries, tasks


def test_a_new_reply_stays_in_view_while_following_and_never_steals_a_scrolled_reader():
    from local_agent.session.textual_hub import HubViewState, SessionHubApp
    from textual.containers import VerticalScroll

    async def scenario():
        entries, tasks = _long_conversation(12)
        app = SessionHubApp(HubViewState(conversation=entries, tasks=tasks))
        async with app.run_test(size=(120, 30)) as pilot:
            pane = app.query_one("#conversation-pane", VerticalScroll)
            await pilot.pause()
            assert pane.max_scroll_y > 0 and pane.scroll_y == pane.max_scroll_y, \
                "an opened conversation does not show its latest reply"
            more, more_tasks = _long_conversation(14)
            app.replace_state(HubViewState(conversation=more, tasks=more_tasks))
            await pilot.pause()
            await pilot.pause()
            assert pane.scroll_y == pane.max_scroll_y, "the newest reply is below the fold"
            pane.scroll_to(y=0, animate=False)
            await pilot.pause()
            most, most_tasks = _long_conversation(16)
            app.replace_state(HubViewState(conversation=most, tasks=most_tasks))
            await pilot.pause()
            await pilot.pause()
            assert pane.scroll_y == 0, "a reader who scrolled up was pulled to the bottom"
    asyncio.run(scenario())
