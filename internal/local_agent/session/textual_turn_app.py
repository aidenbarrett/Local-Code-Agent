"""Composer-to-gateway wiring for the Textual Session Hub.

The UI may request a conversation turn, but it does not execute the gateway inline and it
does not treat the gateway's returned prose as authoritative presentation state. The
canonical conversation provider and durable feed remain responsible for what is rendered.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace

from .textual_dispatch import HubTurnBusy, HubTurnDispatcher, HubTurnDispatcherClosed
from .textual_hub import HubInputSubmitted, HubViewState, SessionHubApp


def _brief_error(exc: BaseException, *, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    text = text[:limit]
    return f"{type(exc).__name__}: {text}"


class DispatchingSessionHubApp(SessionHubApp):
    """Presentation shell whose composer submits through ``HubTurnDispatcher``."""

    def __init__(
        self,
        dispatcher: HubTurnDispatcher,
        state: HubViewState | None = None,
        *,
        palette_name: str = "neon",
        completion_poll_interval: float = 0.05,
    ) -> None:
        if not isinstance(dispatcher, HubTurnDispatcher):
            raise TypeError("dispatching Textual app requires HubTurnDispatcher")
        if (
            not isinstance(completion_poll_interval, (int, float))
            or isinstance(completion_poll_interval, bool)
        ):
            raise TypeError("turn completion poll interval must be numeric")
        if completion_poll_interval <= 0:
            raise ValueError("turn completion poll interval must be positive")
        self.turn_dispatcher = dispatcher
        self.completion_poll_interval = float(completion_poll_interval)
        self._turn_future: Future[str] | None = None
        super().__init__(state, palette_name=palette_name)

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(self.completion_poll_interval, self._poll_turn_completion)

    def _status(self, text: str) -> None:
        self.replace_state(replace(self.view_state, status=text))

    def on_hub_input_submitted(self, message: HubInputSubmitted) -> None:
        """Submit exact composer text without blocking the Textual event loop."""
        if self._turn_future is not None and not self._turn_future.done():
            self._status("Busy · one conversation turn is already running")
            return
        try:
            future = self.turn_dispatcher.submit(message.text)
        except HubTurnBusy:
            self._status("Busy · one conversation turn is already running")
            return
        except HubTurnDispatcherClosed:
            self._status("Turn refused · conversation dispatcher is closed")
            return
        self._turn_future = future
        self._status("Working · conversation turn in progress")

    def _poll_turn_completion(self) -> None:
        """Observe worker completion on the UI thread without trusting returned prose."""
        future = self._turn_future
        if future is None or not future.done():
            return
        self._turn_future = None
        try:
            # Deliberately discard the returned prose here. The persisted conversation
            # projection is the presentation source of truth and will refresh separately.
            future.result()
        except Exception as exc:
            self._status(f"Turn failed · {_brief_error(exc)}")
            return
        self._status("Turn finished · awaiting canonical conversation refresh")


__all__ = ["DispatchingSessionHubApp"]
