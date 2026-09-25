"""Replay-safe durable Session Hub feed for the Textual presentation shell.

The Textual app must never treat an in-memory notification queue as authority. This
adapter owns a durable replay cursor, crosses the replay-to-live handoff explicitly,
and rebuilds presentation state from committed events. Live overflow is recovered by
re-subscribing from the last accepted sequence; it is never smoothed over.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from .session_event_service import DurableSessionService, ReplaySubscription, SubscriptionGap
from .session_store import ArtifactIntegrityError
from .task_history import DurableTaskHistory, RetainedTaskResult
from .textual_hub import ConversationEntry, HubViewState, build_view_state
from .textual_route_read_model import RouteReadModelError, latest_route_snapshot


class HubFeedError(RuntimeError):
    """The durable UI feed cannot establish a truthful contiguous projection."""


ConversationProvider = Callable[[], Sequence[ConversationEntry]]
RouteSummaryProvider = Callable[[], str | None]

_RETAINED_RESULT_UNAVAILABLE = (
    "Unavailable — retained worker result failed integrity validation. "
    "The durable controller verdict above remains authoritative."
)


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
        self._conversation: tuple[ConversationEntry, ...] = ()
        self._result_cache: dict[str, RetainedTaskResult] = {}
        self._retained_results_degraded = False

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def events(self) -> tuple[dict, ...]:
        return tuple(self._events)

    @property
    def recovered_gap(self) -> bool:
        return self._recovered_gap

    def _read_conversation(self) -> tuple[ConversationEntry, ...]:
        conversation = tuple(self.conversation_provider())
        if any(not isinstance(entry, ConversationEntry) for entry in conversation):
            raise TypeError("conversation provider must return ConversationEntry values")
        return conversation

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
        self._conversation = self._read_conversation()
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

        conversation = self._read_conversation()
        if conversation != self._conversation:
            self._conversation = conversation
            changed = True
        return changed

    def _mark_retained_result_unavailable(self, task) -> None:
        """Keep durable controller truth while refusing corrupt sibling prose."""
        self._retained_results_degraded = True
        task.result_answer = _RETAINED_RESULT_UNAVAILABLE
        task.result_verification_ran = None
        task.result_verified_at_completion = None

    def _hydrate_retained_results(self, state: HubViewState) -> HubViewState:
        history = DurableTaskHistory(self.service.store, stream_id=self.service.stream_id)
        for task in state.tasks:
            if not task.terminal:
                continue
            ref = task.result_ref
            digest = ref.get("sha256") if isinstance(ref, dict) else None
            try:
                result = self._result_cache.get(digest) if isinstance(digest, str) else None
                if result is None:
                    result = history.result_for_task(task.task_id)
                    if result is None:
                        self._mark_retained_result_unavailable(task)
                        continue
                    if isinstance(digest, str):
                        self._result_cache[digest] = result
                if result.task_id != task.task_id:
                    raise ArtifactIntegrityError(
                        "retained task result cache identity disagrees with durable task projection"
                    )
                if result.status != task.state or result.verdict != task.verdict:
                    raise ArtifactIntegrityError(
                        "retained task result disagrees with durable task projection"
                    )
                if result.evidence_ids != task.evidence_ids:
                    raise ArtifactIntegrityError(
                        "retained task result evidence disagrees with durable verdict"
                    )
            except ArtifactIntegrityError:
                self._mark_retained_result_unavailable(task)
                continue
            task.result_answer = result.answer
            task.result_verification_ran = result.verification_ran
            task.result_verified_at_completion = result.verified_at_completion
        return state

    def state(self, *, status: str | None = None) -> HubViewState:
        if not self._started:
            raise HubFeedError("durable UI feed must be started before projection")
        try:
            latest_route = latest_route_snapshot(self._events)
            route_summary = (
                None if latest_route is None else latest_route.summary()
            ) if self.route_summary_provider is None else self.route_summary_provider()
        except RouteReadModelError as exc:
            raise HubFeedError("durable route history cannot be projected truthfully") from exc
        if status is None:
            if (
                self.route_summary_provider is None
                and latest_route is not None
                and latest_route.pending
            ):
                status = "Decision required · reply work to accept · chat to keep conversation-only"
            else:
                status = f"Live · durable seq {self._cursor}"
                if self._recovered_gap:
                    status += " · replay recovered"
                if self._retained_results_degraded:
                    status += " · retained result unavailable"
        state = build_view_state(
            self._conversation,
            self._events,
            route_summary=route_summary,
            status=status,
        )
        return self._hydrate_retained_results(state)


__all__ = ["DurableHubFeed", "HubFeedError"]
