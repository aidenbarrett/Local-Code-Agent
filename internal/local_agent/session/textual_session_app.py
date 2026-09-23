"""Live Session Hub composition for durable presentation plus non-blocking turns.

The Textual client may submit conversation input, but the gateway still owns turn
semantics and the durable feed remains presentation authority. Returned gateway prose is
never injected directly into the UI. After a turn completes, the app refreshes from the
canonical conversation provider and committed durable stream.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace

from textual.widgets import Input, Static

from .textual_dispatch import HubTurnBusy, HubTurnDispatcher, HubTurnDispatcherClosed
from .textual_feed import DurableHubFeed, HubFeedError
from .textual_hub import HubInputSubmitted, HubViewState
from .textual_live_app import LiveSessionHubApp


def _brief_error(exc: BaseException, *, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:limit]}"


class LiveDispatchingSessionHubApp(LiveSessionHubApp):
    """Drive one Textual Hub from durable state while dispatching turns off-thread."""

    def __init__(
        self,
        feed: DurableHubFeed,
        dispatcher: HubTurnDispatcher,
        *,
        palette_name: str = "neon",
        poll_interval: float = 0.10,
        completion_poll_interval: float = 0.05,
        runtime_summary: str | None = None,
    ) -> None:
        if not isinstance(dispatcher, HubTurnDispatcher):
            raise TypeError("live dispatching Textual app requires HubTurnDispatcher")
        if (
            not isinstance(completion_poll_interval, (int, float))
            or isinstance(completion_poll_interval, bool)
        ):
            raise TypeError("turn completion poll interval must be numeric")
        if completion_poll_interval <= 0:
            raise ValueError("turn completion poll interval must be positive")
        if runtime_summary is not None and (not isinstance(runtime_summary, str) or not runtime_summary.strip()):
            raise ValueError("runtime_summary must be nonempty text when supplied")
        self.turn_dispatcher = dispatcher
        self.completion_poll_interval = float(completion_poll_interval)
        self.runtime_summary = runtime_summary
        self._turn_future: Future[str] | None = None
        self._turn_status: str | None = None
        super().__init__(feed, palette_name=palette_name, poll_interval=poll_interval)

    def on_mount(self) -> None:
        super().on_mount()
        if self.runtime_summary:
            self.query_one("#title", Static).update(
                "LOCAL CODE AGENT · SESSION HUB  ·  " + self.runtime_summary
            )
        self.set_interval(self.completion_poll_interval, self._poll_turn_completion)

    def _fail_feed(self, exc: HubFeedError) -> None:
        self._feed_failed = True
        self._turn_status = None
        self.replace_state(
            HubViewState(
                conversation=self.view_state.conversation,
                tasks=self.view_state.tasks,
                watches=self.view_state.watches,
                route_summary=self.view_state.route_summary,
                status=f"Durable feed error · {exc}",
            )
        )

    def _poll_live_feed(self) -> None:
        """Refresh durable state without hiding an in-flight turn status."""
        if self._feed_failed:
            return
        try:
            state = self.live_binding.poll()
        except HubFeedError as exc:
            self._fail_feed(exc)
            return
        if state is None:
            return
        if self._turn_status is not None:
            state = replace(state, status=self._turn_status)
        self.replace_state(state)

    def _status(self, text: str) -> None:
        self._turn_status = text
        self.replace_state(replace(self.view_state, status=text))

    def _restore_draft(self, text: str) -> None:
        """Put rejected composer text back; refusal must never silently eat user input."""
        self.query_one("#composer", Input).value = text

    def on_hub_input_submitted(self, message: HubInputSubmitted) -> None:
        """Submit exact composer text without blocking Textual or inventing UI state."""
        normalized = message.text.strip().lower()
        if normalized in {"/quit", "/exit"}:
            if self._turn_future is not None and not self._turn_future.done():
                self._restore_draft(message.text)
                self._status("Quit refused · conversation turn is still running")
                return
            self._turn_status = None
            self.exit()
            return

        if self._feed_failed:
            self._restore_draft(message.text)
            self._status("Turn refused · durable feed is unavailable; restart to resnapshot")
            return

        if self._turn_future is not None and not self._turn_future.done():
            self._restore_draft(message.text)
            self._status("Busy · one conversation turn is already running")
            return
        try:
            future = self.turn_dispatcher.submit(message.text)
        except HubTurnBusy:
            self._restore_draft(message.text)
            self._status("Busy · one conversation turn is already running")
            return
        except HubTurnDispatcherClosed:
            self._restore_draft(message.text)
            self._status("Turn refused · conversation dispatcher is closed")
            return
        self._turn_future = future
        self._status("Working · conversation turn in progress")

    def _poll_turn_completion(self) -> None:
        """Refresh canonical state after completion; never trust returned prose as UI data."""
        future = self._turn_future
        if future is None or not future.done():
            return
        self._turn_future = None
        try:
            future.result()
        except Exception as exc:
            self._status(f"Turn failed · {_brief_error(exc)}")
            return

        self._turn_status = None
        if self._feed_failed:
            return
        try:
            self.live_binding.poll()
        except HubFeedError as exc:
            self._fail_feed(exc)
            return
        self.replace_state(self.live_binding.state)


__all__ = ["LiveDispatchingSessionHubApp"]
