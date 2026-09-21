"""Live Textual binding over the replay-safe durable Hub feed.

This module still grants no policy or execution authority. It only keeps the existing
presentation shell synchronized with the committed durable stream and the canonical
conversation provider owned by ``DurableHubFeed``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .textual_feed import DurableHubFeed, HubFeedError
from .textual_hub import HubViewState, SessionHubApp


@dataclass
class HubLiveBinding:
    """Synchronize immutable Hub view state without making the UI authoritative."""

    feed: DurableHubFeed

    def __post_init__(self) -> None:
        if not isinstance(self.feed, DurableHubFeed):
            raise TypeError("live Textual binding requires DurableHubFeed")
        self._state = self.feed.start()

    @property
    def state(self) -> HubViewState:
        return self._state

    def poll(self) -> HubViewState | None:
        """Return a replacement state only when durable/conversation state changed."""
        durable_changed = self.feed.poll()
        projected = self.feed.state()
        if durable_changed or projected != self._state:
            self._state = projected
            return projected
        return None


class LiveSessionHubApp(SessionHubApp):
    """Session Hub presentation shell driven by committed durable replay/live state."""

    def __init__(
        self,
        feed: DurableHubFeed,
        *,
        palette_name: str = "neon",
        poll_interval: float = 0.10,
    ) -> None:
        if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool):
            raise TypeError("Textual feed poll interval must be numeric")
        if poll_interval <= 0:
            raise ValueError("Textual feed poll interval must be positive")
        self.live_binding = HubLiveBinding(feed)
        self.poll_interval = float(poll_interval)
        self._feed_failed = False
        super().__init__(self.live_binding.state, palette_name=palette_name)

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(self.poll_interval, self._poll_live_feed)

    def _poll_live_feed(self) -> None:
        if self._feed_failed:
            return
        try:
            state = self.live_binding.poll()
        except HubFeedError as exc:
            self._feed_failed = True
            failed = HubViewState(
                conversation=self.view_state.conversation,
                tasks=self.view_state.tasks,
                watches=self.view_state.watches,
                route_summary=self.view_state.route_summary,
                status=f"Durable feed error · {exc}",
            )
            self.replace_state(failed)
            return
        if state is not None:
            self.replace_state(state)


__all__ = ["HubLiveBinding", "LiveSessionHubApp"]
