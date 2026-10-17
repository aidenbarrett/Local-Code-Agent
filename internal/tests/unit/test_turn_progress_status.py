"""While a turn runs, the status line says how long and which recorded step is active.

Real local-model turns take tens of seconds to minutes. A static "Working" line
reads as a hang. The status names only recorded tool activity and the elapsed time
since the turn was submitted; Esc stops the active durable task.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_read_model import TaskActivity, TaskSnapshot, ToolActivity
from local_agent.session.textual_dispatch import HubTurnDispatcher
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import HubInputSubmitted, HubViewState
from local_agent.session.textual_session_app import LiveDispatchingSessionHubApp, working_status


def _task(**changes) -> TaskSnapshot:
    base = TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=1, last_sequence=2, state="running",
        execution_epoch=3, origin_kind="user_rule", repository_id="repo", skill="fix-build-failure",
        deadline_utc="2030-01-01T00:00:00Z",
    )
    return replace(base, **changes)


def _finished(n: int) -> list[TaskActivity]:
    return [TaskActivity(i, "2026-10-17T00:00:00Z", "tool.finished", "") for i in range(n)]


def test_status_names_the_running_tool_and_elapsed_time():
    task = _task(open_tools={"c1": ToolActivity("c1", "build_target")})
    assert working_status(HubViewState(tasks=(task,)), 75.9) == \
        "Working 1:15 · running build_target · Esc to stop"


def test_between_tools_the_status_claims_only_what_finished():
    task = _task(last_tool=ToolActivity("c2", "propose_patch"), activity=_finished(2))
    assert working_status(HubViewState(tasks=(task,)), 9) == \
        "Working 0:09 · 2 steps done, last propose_patch · Esc to stop"
    assert "model" not in working_status(HubViewState(tasks=(task,)), 9)


def test_a_task_with_no_tool_yet_reports_its_state():
    assert working_status(HubViewState(tasks=(_task(),)), 0) == \
        "Working 0:00 · task running · Esc to stop"


def test_a_turn_with_no_active_task_offers_no_stop():
    closed = _task(closed_sequence=5, state="completed")
    assert working_status(HubViewState(tasks=(closed,)), 3) == \
        "Working 0:03 · conversation turn in progress"


def test_many_running_tools_are_summarised():
    tools = {f"c{i}": ToolActivity(f"c{i}", f"tool{i}") for i in range(4)}
    assert "running tool0, tool1 +2" in working_status(HubViewState(tasks=(_task(open_tools=tools),)), 1)


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                 stream_id=str(uuid4()), session_id=str(uuid4()))


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def test_the_live_status_ticks_and_esc_stops_the_active_task(tmp_path):
    async def scenario():
        service = _service(tmp_path)
        release = threading.Event()

        class Gateway:
            def turn(self, text, *, explicit_mode=None):
                assert release.wait(10)
                return "unused"

        dispatcher = HubTurnDispatcher(Gateway())
        stops: list[tuple[str, int]] = []
        app = LiveDispatchingSessionHubApp(
            DurableHubFeed(service), dispatcher, poll_interval=0.02,
            completion_poll_interval=0.02,
            stop_task=lambda task_id, epoch: stops.append((task_id, epoch)),
        )
        clock = [100.0]
        app._clock = lambda: clock[0]
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                # A slow runner can hand control back before the Hub has composed its
                # widgets; submit only once the screen the turn will update exists.
                await _wait_until(lambda: bool(app.query("#task-select")))
                app.post_message(HubInputSubmitted("fix it"))
                await _wait_until(lambda: app.view_state.status.startswith("Working 0:00"))
                running = _task(open_tools={"c1": ToolActivity("c1", "build_target")})
                app.replace_state(HubViewState(tasks=(running,), status="Live"))
                clock[0] += 65
                await _wait_until(lambda: app.view_state.status
                                  == "Working 1:05 · running build_target · Esc to stop")
                await pilot.press("escape")
                await pilot.pause()
                assert stops == [(running.task_id, 3)]
                assert app.view_state.status == f"Stop requested · task {running.task_id}"
                release.set()
                # Leave the app only after it has processed the turn's completion: a
                # completion poll still pending at teardown would update widgets the
                # closing screen no longer has.
                await _wait_until(lambda: not dispatcher.busy and app._turn_future is None)
                await pilot.pause()
        finally:
            release.set()
            dispatcher.close()
            service.close()

    asyncio.run(scenario())


def test_esc_with_no_active_task_does_nothing(tmp_path):
    async def scenario():
        service = _service(tmp_path)

        class Gateway:
            def turn(self, text, *, explicit_mode=None):
                return "unused"

        dispatcher = HubTurnDispatcher(Gateway())
        stops: list[object] = []
        app = LiveDispatchingSessionHubApp(DurableHubFeed(service), dispatcher,
                                           stop_task=lambda *a: stops.append(a))
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await _wait_until(lambda: bool(app.query("#task-select")))
                before = app.view_state.status
                await pilot.press("escape")
                await pilot.pause()
                assert stops == [] and app.view_state.status == before
        finally:
            dispatcher.close()
            service.close()

    asyncio.run(scenario())
