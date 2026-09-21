from __future__ import annotations

import threading

import pytest

from local_agent.session.textual_dispatch import (
    HubTurnBusy,
    HubTurnDispatcher,
    HubTurnDispatcherClosed,
)


class BlockingGateway:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def turn(self, text, *, explicit_mode=None):
        self.calls.append((text, explicit_mode))
        self.entered.set()
        assert self.release.wait(3)
        return "done:" + text


def test_submit_returns_future_while_gateway_turn_runs_off_caller_thread():
    gateway = BlockingGateway()
    dispatcher = HubTurnDispatcher(gateway)
    try:
        future = dispatcher.submit("inspect", explicit_mode="work")
        assert gateway.entered.wait(1)
        assert future.done() is False
        assert dispatcher.busy is True
        gateway.release.set()
        assert future.result(3) == "done:inspect"
        assert gateway.calls == [("inspect", "work")]
        assert dispatcher.busy is False
    finally:
        gateway.release.set()
        dispatcher.close()


def test_second_conversation_turn_is_refused_instead_of_hidden_queueing():
    gateway = BlockingGateway()
    dispatcher = HubTurnDispatcher(gateway)
    try:
        first = dispatcher.submit("one")
        assert gateway.entered.wait(1)
        with pytest.raises(HubTurnBusy, match="already processing"):
            dispatcher.submit("two")
        assert gateway.calls == [("one", None)]
        gateway.release.set()
        assert first.result(3) == "done:one"
    finally:
        gateway.release.set()
        dispatcher.close()


def test_gateway_exception_remains_visible_on_returned_future():
    class BrokenGateway:
        def turn(self, text, *, explicit_mode=None):
            raise RuntimeError("model unavailable")

    dispatcher = HubTurnDispatcher(BrokenGateway())
    try:
        future = dispatcher.submit("hello")
        with pytest.raises(RuntimeError, match="model unavailable"):
            future.result(3)
    finally:
        dispatcher.close()


def test_close_refuses_new_turns_without_cancelling_started_turn():
    gateway = BlockingGateway()
    dispatcher = HubTurnDispatcher(gateway)
    future = dispatcher.submit("finish me")
    assert gateway.entered.wait(1)

    closed = threading.Event()

    def close_dispatcher():
        dispatcher.close()
        closed.set()

    closer = threading.Thread(target=close_dispatcher)
    closer.start()
    assert closed.wait(0.05) is False
    with pytest.raises(HubTurnDispatcherClosed, match="closed"):
        dispatcher.submit("too late")

    gateway.release.set()
    assert future.result(3) == "done:finish me"
    assert closed.wait(2)
    closer.join(2)
    assert not closer.is_alive()


def test_dispatcher_requires_turn_capability():
    with pytest.raises(TypeError, match="turn-capable"):
        HubTurnDispatcher(object())
