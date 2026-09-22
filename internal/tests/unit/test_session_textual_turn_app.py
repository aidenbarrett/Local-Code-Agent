from __future__ import annotations

import asyncio
import threading

from local_agent.session.textual_dispatch import HubTurnDispatcher
from local_agent.session.textual_hub import ConversationEntry, HubInputSubmitted, HubViewState
from local_agent.session.textual_turn_app import DispatchingSessionHubApp


class BlockingGateway:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def turn(self, text, *, explicit_mode=None):
        self.calls.append((text, explicit_mode))
        self.entered.set()
        assert self.release.wait(3)
        return "model prose that must not be injected directly"


def test_composer_dispatch_preserves_exact_text_and_refuses_hidden_second_turn():
    async def scenario() -> None:
        gateway = BlockingGateway()
        dispatcher = HubTurnDispatcher(gateway)
        app = DispatchingSessionHubApp(
            dispatcher,
            HubViewState(conversation=(ConversationEntry("assistant", "existing"),)),
            completion_poll_interval=0.01,
        )
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.post_message(HubInputSubmitted("  hello exactly  "))
                await pilot.pause()
                assert gateway.entered.wait(1)
                assert gateway.calls == [("  hello exactly  ", None)]
                assert app.view_state.status == "Working · conversation turn in progress"

                app.post_message(HubInputSubmitted("second"))
                await pilot.pause()
                assert app.view_state.status == "Busy · one conversation turn is already running"
                assert gateway.calls == [("  hello exactly  ", None)]

                gateway.release.set()
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert app.view_state.status == "Turn finished · awaiting canonical conversation refresh"
                assert app.view_state.conversation == (ConversationEntry("assistant", "existing"),)
        finally:
            gateway.release.set()
            dispatcher.close()

    asyncio.run(scenario())


def test_gateway_failure_is_status_not_success_shaped_conversation():
    class BrokenGateway:
        def turn(self, text, *, explicit_mode=None):
            raise RuntimeError("endpoint unavailable")

    async def scenario() -> None:
        dispatcher = HubTurnDispatcher(BrokenGateway())
        app = DispatchingSessionHubApp(dispatcher, completion_poll_interval=0.01)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.post_message(HubInputSubmitted("hello"))
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert app.view_state.conversation == ()
                assert app.view_state.status == "Turn failed · RuntimeError: endpoint unavailable"
        finally:
            dispatcher.close()

    asyncio.run(scenario())


def test_closed_dispatcher_refuses_composer_turn_visibly():
    class Gateway:
        def turn(self, text, *, explicit_mode=None):
            return "unused"

    async def scenario() -> None:
        dispatcher = HubTurnDispatcher(Gateway())
        dispatcher.close()
        app = DispatchingSessionHubApp(dispatcher)
        async with app.run_test(size=(100, 30)) as pilot:
            app.post_message(HubInputSubmitted("hello"))
            await pilot.pause()
            assert app.view_state.status == "Turn refused · conversation dispatcher is closed"

    asyncio.run(scenario())
