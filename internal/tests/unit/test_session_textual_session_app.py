from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_dispatch import HubTurnDispatcher
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import ConversationEntry, HubInputSubmitted
from local_agent.session.textual_session_app import LiveDispatchingSessionHubApp


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _opened(service: DurableSessionService, label: str) -> None:
    receipt = service.append(
        "session.opened",
        {
            "conversation_id": label,
            "repository_id": "repo-1",
            "controller_commit": "fixture",
            "capabilities": [],
            "recovered": False,
        },
    )
    receipt.wait(5)


def test_completed_turn_refreshes_canonical_conversation_not_returned_prose(tmp_path):
    async def scenario() -> None:
        service = _service(tmp_path)
        conversation: list[ConversationEntry] = []

        class Gateway:
            def turn(self, text, *, explicit_mode=None):
                conversation.extend(
                    (ConversationEntry("user", text), ConversationEntry("assistant", "persisted answer"))
                )
                _opened(service, "after-turn")
                return "returned prose must not be injected"

        dispatcher = HubTurnDispatcher(Gateway())
        app = LiveDispatchingSessionHubApp(
            DurableHubFeed(service, conversation_provider=lambda: tuple(conversation)),
            dispatcher,
            poll_interval=0.01,
            completion_poll_interval=0.01,
        )
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.post_message(HubInputSubmitted("hello exactly"))
                await asyncio.sleep(0.08)
                await pilot.pause()
                assert [entry.text for entry in app.view_state.conversation] == [
                    "hello exactly",
                    "persisted answer",
                ]
                assert "returned prose" not in "\n".join(
                    entry.text for entry in app.view_state.conversation
                )
                assert app.live_binding.state.status == "Live · durable seq 1"
                assert app.view_state.status == "Live · durable seq 1"
        finally:
            dispatcher.close()
            service.close()

    asyncio.run(scenario())


def test_live_feed_advances_without_hiding_inflight_turn_status(tmp_path):
    async def scenario() -> None:
        service = _service(tmp_path)

        class BlockingGateway:
            def __init__(self) -> None:
                self.entered = threading.Event()
                self.release = threading.Event()

            def turn(self, text, *, explicit_mode=None):
                self.entered.set()
                assert self.release.wait(3)
                return "unused"

        gateway = BlockingGateway()
        dispatcher = HubTurnDispatcher(gateway)
        app = LiveDispatchingSessionHubApp(
            DurableHubFeed(service),
            dispatcher,
            poll_interval=0.01,
            completion_poll_interval=0.01,
        )
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.post_message(HubInputSubmitted("work"))
                await pilot.pause()
                assert gateway.entered.wait(1)
                _opened(service, "while-busy")
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert app.live_binding.state.status == "Live · durable seq 1"
                assert app.view_state.status == "Working · conversation turn in progress"
                gateway.release.set()
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert app.view_state.status == "Live · durable seq 1"
        finally:
            gateway.release.set()
            dispatcher.close()
            service.close()

    asyncio.run(scenario())


def test_combined_app_refuses_closed_dispatcher_visibly(tmp_path):
    async def scenario() -> None:
        service = _service(tmp_path)

        class Gateway:
            def turn(self, text, *, explicit_mode=None):
                return "unused"

        dispatcher = HubTurnDispatcher(Gateway())
        dispatcher.close()
        app = LiveDispatchingSessionHubApp(DurableHubFeed(service), dispatcher)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.post_message(HubInputSubmitted("hello"))
                await pilot.pause()
                assert app.view_state.status == "Turn refused · conversation dispatcher is closed"
        finally:
            service.close()

    asyncio.run(scenario())


def test_quit_is_owned_by_textual_session_and_never_dispatched(tmp_path):
    service = _service(tmp_path)

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def turn(self, text, *, explicit_mode=None):
            self.calls.append(text)
            return "unused"

    gateway = Gateway()
    dispatcher = HubTurnDispatcher(gateway)
    app = LiveDispatchingSessionHubApp(DurableHubFeed(service), dispatcher)
    exited: list[bool] = []
    app.exit = lambda *args, **kwargs: exited.append(True)  # type: ignore[method-assign]
    try:
        app.on_hub_input_submitted(HubInputSubmitted(" /QUIT "))
        assert exited == [True]
        assert gateway.calls == []
        assert dispatcher.busy is False
    finally:
        dispatcher.close()
        service.close()
