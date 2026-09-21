"""Replay-safe durable Session Hub feed for the Textual presentation shell.

The Textual app must never treat an in-memory notification queue as authority. This
adapter owns a durable replay cursor, crosses the replay-to-live handoff explicitly,
and rebuilds presentation state from committed events. Live overflow is recovered by
re-subscribing from the last accepted durable sequence; it is never smoothed over.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from .session_event_service import DurableSessionService, ReplaySubscription, SubscriptionGap
from .textual_hub import ConversationEntry, HubViewState, build_view_state
from .textual_route_read_model import RouteReadModelError, latest_route_summary


class HubFeedError(RuntimeError):
    """The durable UI feed cannot establish a truthful contiguous projection."""


ConversationProvider = Callable[[], Sequence[ConversationEntry]]
RouteSummaryProvider = Callable[[], str | None]


class DurableHubFeed:
    """Project one durable stream into immutable Textual view state.

    ``start`` performs the authoritative replay before callers render anything.
    ``poll`` is non-blocking in the healthy live path. A subscription overflow causes
    an explicit replay from the last accepted sequence before any newer state is shown.
    """

    def __init__(
        self,
        service: DurableSessionService,
        *,
        conversation_provider: ConversationProvider | None = None,
        route_summary_provider: RouteSummaryProvider | None = None,
        capacity: int = 512,
        replay_page: int = 1000,
        max_events: int = 20_000,
    ) -> None:
        if not isinstance(service, DurableSessionService):
            raise TypeError("Textual live feed requires DurableSessionService")
        if capacity < 1:
            raise ValueError("feed subscription capacity must be positive")
        if replay_page < 1:
            raise ValueError("feed replay page must be positive")
        if max_events < 1:
            raise ValueError("feed event limit must be positive")
        self.service = service
        self.conversation_provider = conversation_provider or (lambda: ())
        self.route_summary_provider = route_summary_provider
        self.capacity = capacity
        self.replay_page = replay_page
        self.max_events = max_events
        self._events: list[dict] = []
        self._cursor = 0
        self._handoff: ReplaySubscription | None = None
        self._started = False
        self._recovered_gap = False

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def events(self) -> tuple[dict, ...]:
        return tuple(self._events)

    @property
    def recovered_gap(self) -> bool:
        return self._recovered_gap

    def _accept(self, events: Sequence[dict]) -> int:
        accepted = 0
        for event in events:
            if event.get("stream_id") != self.service.stream_id:
                raise HubFeedError("durable UI feed received an event from another stream")
            sequence = int(event["sequence"])
            expected = self._cursor + 1
            if sequence != expected:
                raise HubFeedError(
                    f"durable UI feed expected sequence {expected}, received {sequence}"
                )
            if len(self._events) >= self.max_events:
                raise HubFeedError(
                    "durable UI feed exceeded its bounded event history; restart with a larger bound"
                )
            self._events.append(event)
            self._cursor = sequence
            accepted += 1
        return accepted

    def _catch_up(self, handoff: ReplaySubscription) -> int:
        accepted = 0
        while self._cursor < handoff.replay_through:
            batch = self.service.replay_subscription(
                handoff,
                after=self._cursor,
                limit=self.replay_page,
            )
            if not batch:
                raise HubFeedError(
                    "durable replay stopped before the replay-to-live boundary"
                )
            accepted += self._accept(batch)
        if self._cursor != handoff.replay_through:
            raise HubFeedError("durable replay crossed its declared handoff boundary")
        return accepted

    def start(self) -> HubViewState:
        if self._started:
            raise HubFeedError("durable UI feed is already started")
        handoff = self.service.subscribe_from(after=0, capacity=self.capacity)
        self._handoff = handoff
        self._catch_up(handoff)
        self._started = True
        return self.state()

    def _recover_gap(self) -> int:
        handoff = self.service.subscribe_from(after=self._cursor, capacity=self.capacity)
        self._handoff = handoff
        accepted = self._catch_up(handoff)
        self._recovered_gap = True
        return accepted

    def poll(self, *, limit: int = 256) -> bool:
        if not self._started or self._handoff is None:
            raise HubFeedError("durable UI feed must be started before polling")
        if limit < 1:
            raise ValueError("feed poll limit must be positive")
        changed = False
        try:
            batch = self._handoff.live.drain(limit=limit)
        except SubscriptionGap:
            changed = bool(self._recover_gap())
            assert self._handoff is not None
            batch = self._handoff.live.drain(limit=limit)
        if batch:
            self._accept(batch)
            changed = True
        return changed

    def state(self, *, status: str | None = None) -> HubViewState:
        if not self._started:
            raise HubFeedError("durable UI feed must be started before projection")
        conversation = tuple(self.conversation_provider())
        if any(not isinstance(entry, ConversationEntry) for entry in conversation):
            raise TypeError("conversation provider must return ConversationEntry values")
        try:
            route_summary = (
                latest_route_summary(self._events)
                if self.route_summary_provider is None
                else self.route_summary_provider()
            )
        except RouteReadModelError as exc:
            raise HubFeedError("durable route history cannot be projected truthfully") from exc
        if status is None:
            status = f"Live · durable seq {self._cursor}"
            if self._recovered_gap:
                status += " · replay recovered"
        return build_view_state(
            conversation,
            self._events,
            route_summary=route_summary,
            status=status,
        )


__all__ = ["DurableHubFeed", "HubFeedError"]
