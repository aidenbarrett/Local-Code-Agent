"""Live Session Hub composition for durable presentation plus non-blocking turns.

The Textual client may submit conversation input, but the gateway still owns turn
semantics and the durable feed remains presentation authority. Returned gateway prose is
never injected directly into the UI. After a turn completes, the app refreshes from the
canonical conversation provider and committed durable stream.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
import time
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.widgets import Input, Static

from .textual_dispatch import HubTurnBusy, HubTurnDispatcher, HubTurnDispatcherClosed
from .textual_feed import DurableHubFeed, HubFeedError
from .task_read_model import TaskSnapshot
from .textual_hub import HubInputSubmitted, HubViewState
from .textual_live_app import LiveSessionHubApp


StopTask = Callable[[str, int], object]


def _brief_error(exc: BaseException, *, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:limit]}"


def _active_task(state: HubViewState) -> TaskSnapshot | None:
    return next((item for item in reversed(state.tasks) if not item.terminal), None)


def working_status(state: HubViewState, elapsed_s: float) -> str:
    """What an in-flight turn is doing, from durable task facts and local elapsed time.

    Only recorded tool activity is named. Between tools nothing is claimed about the
    model; the status says which step finished last.
    """
    minutes, seconds = divmod(max(0, int(elapsed_s)), 60)
    parts = [f"Working {minutes}:{seconds:02d}"]
    task = _active_task(state)
    if task is None:
        parts.append("conversation turn in progress")
    else:
        running = task.frozen_open_tools()
        finished = sum(1 for item in task.activity if item.kind == "tool.finished")
        if running:
            names = ", ".join(tool.tool_name for tool in running[:2])
            more = f" +{len(running) - 2}" if len(running) > 2 else ""
            parts.append(f"running {names}{more}")
        elif task.last_tool is not None:
            parts.append(f"{finished} step{'s' if finished != 1 else ''} done, "
                         f"last {task.last_tool.tool_name}")
        else:
            parts.append(f"task {task.state}")
        parts.append("Esc to stop")
    return " · ".join(parts)


class LiveDispatchingSessionHubApp(LiveSessionHubApp):
    """Drive one Textual Hub from durable state while dispatching turns off-thread."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "stop_task", "Stop", show=False)]

    def __init__(
        self,
        feed: DurableHubFeed,
        dispatcher: HubTurnDispatcher,
        *,
        stop_task: StopTask | None = None,
        palette_name: str = "neon",
        poll_interval: float = 0.10,
        completion_poll_interval: float = 0.05,
        runtime_summary: str | None = None,
    ) -> None:
        if not isinstance(dispatcher, HubTurnDispatcher):
            raise TypeError("live dispatching Textual app requires HubTurnDispatcher")
        if stop_task is not None and not callable(stop_task):
            raise TypeError("stop_task must be callable when supplied")
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
        self.stop_task = stop_task
        self.completion_poll_interval = float(completion_poll_interval)
        self.runtime_summary = runtime_summary
        self._turn_future: Future[str] | None = None
        self._turn_status: str | None = None
        self._turn_started: float | None = None
        self._clock: Callable[[], float] = time.monotonic
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

    def _request_stop(self) -> None:
        if self.stop_task is None:
            self._status("Stop unavailable · cancellation authority is not connected")
            return
        task = next((item for item in reversed(self.view_state.tasks) if not item.terminal), None)
        if task is None:
            self._status("Stop unavailable · no active task")
            return
        try:
            self.stop_task(task.task_id, task.execution_epoch)
        except Exception as exc:
            self._status(f"Stop failed · {_brief_error(exc)}")
            return
        self._status(f"Stop requested · task {task.task_id}")

    def on_hub_input_submitted(self, message: HubInputSubmitted) -> None:
        """Submit exact composer text without blocking Textual or inventing UI state."""
        normalized = message.text.strip().lower()
        if normalized in {"stop", "/stop"}:
            self._request_stop()
            return
        if normalized in {"/quit", "/exit"}:
            if self._turn_future is not None and not self._turn_future.done():
                if message.from_composer:
                    self._restore_draft(message.text)
                self._status("Quit refused · conversation turn is still running")
                return
            self._turn_status = None
            self.exit()
            return

        if self._feed_failed:
            if message.from_composer:
                self._restore_draft(message.text)
            self._status("Turn refused · durable feed is unavailable; restart to resnapshot")
            return

        if self._turn_future is not None and not self._turn_future.done():
            if message.from_composer:
                self._restore_draft(message.text)
            self._status("Busy · one conversation turn is already running")
            return
        try:
            future = self.turn_dispatcher.submit(message.text)
        except HubTurnBusy:
            if message.from_composer:
                self._restore_draft(message.text)
            self._status("Busy · one conversation turn is already running")
            return
        except HubTurnDispatcherClosed:
            if message.from_composer:
                self._restore_draft(message.text)
            self._status("Turn refused · conversation dispatcher is closed")
            return
        self._turn_future = future
        self._turn_started = self._clock()
        self._status(working_status(self.view_state, 0.0))

    def action_stop_task(self) -> None:
        """Esc stops the active durable task; with none running it does nothing."""
        if _active_task(self.view_state) is not None:
            self._request_stop()

    def _poll_turn_completion(self) -> None:
        """Refresh canonical state after completion; never trust returned prose as UI data."""
        future = self._turn_future
        if future is None:
            return
        if not future.done():
            started = self._turn_started
            if (started is not None and self._turn_status is not None
                    and self._turn_status.startswith("Working ")):
                current = working_status(self.view_state, self._clock() - started)
                if current != self._turn_status:
                    self._status(current)
            return
        self._turn_future = None
        self._turn_started = None
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
