"""Deterministic in-process arbitration for shared inference endpoints.

The scheduler owns runtime access to a physical endpoint identity. It does not choose
models, routes or task policy. One endpoint has one active lease by default; additional
callers wait FIFO behind a bounded queue and an explicit deadline. Queued cancellation
and endpoint quarantine are observable state transitions rather than hidden retries.

This is deliberately non-preemptive: releasing an active lease is the only normal way to
hand the endpoint to another caller. Cancellation of an already-active inference call
belongs to the runtime/client cancellation path, not queue bookkeeping.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Condition
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID


class EndpointLeaseError(RuntimeError):
    pass


class EndpointQueueFull(EndpointLeaseError):
    pass


class EndpointLeaseTimeout(EndpointLeaseError):
    pass


class EndpointRequestCancelled(EndpointLeaseError):
    pass


class EndpointQuarantined(EndpointLeaseError):
    pass


def _require_uuid(name: str, value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _nonempty(name: str, value: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")
    normalized = value.strip()
    if len(normalized) > limit:
        raise ValueError(f"{name} exceeds size limit")
    return normalized


def _normalize_endpoint(value: str) -> str:
    raw = _nonempty("endpoint URL", value, limit=2048)
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("endpoint URL must use http or https")
    if not parsed.hostname:
        raise ValueError("endpoint URL requires a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("endpoint URL must not embed credentials")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = "" if parsed.port is None else f":{parsed.port}"
    netloc = host + port
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


@dataclass(frozen=True)
class EndpointIdentity:
    """Physical endpoint identity, independent of friendly profile names."""

    base_url: str
    model: str
    revision: str
    device: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _normalize_endpoint(self.base_url))
        object.__setattr__(self, "model", _nonempty("model", self.model))
        object.__setattr__(self, "revision", _nonempty("model revision", self.revision))
        object.__setattr__(self, "device", _nonempty("device", self.device).lower())


@dataclass(frozen=True)
class EndpointQueueSnapshot:
    active_request_id: str | None
    queued_request_ids: tuple[str, ...]
    quarantined_reason: str | None


@dataclass
class _EndpointState:
    active_request_id: str | None = None
    queued_request_ids: deque[str] | None = None
    quarantined_reason: str | None = None

    def queue(self) -> deque[str]:
        if self.queued_request_ids is None:
            self.queued_request_ids = deque()
        return self.queued_request_ids


class EndpointLease:
    """One non-preemptive active endpoint lease."""

    def __init__(self, scheduler: "EndpointLeaseScheduler", identity: EndpointIdentity, request_id: str):
        self._scheduler = scheduler
        self.identity = identity
        self.request_id = request_id
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._scheduler._release(self.identity, self.request_id)
        self._released = True

    def __enter__(self) -> "EndpointLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


class EndpointLeaseScheduler:
    """Bounded FIFO lease scheduler with explicit cancellation and quarantine."""

    def __init__(self, *, max_queue_per_endpoint: int = 8, clock: Callable[[], float] = time.monotonic):
        if (
            not isinstance(max_queue_per_endpoint, int)
            or isinstance(max_queue_per_endpoint, bool)
            or max_queue_per_endpoint < 0
        ):
            raise ValueError("max endpoint queue must be a nonnegative integer")
        if not callable(clock):
            raise TypeError("endpoint scheduler clock must be callable")
        self.max_queue_per_endpoint = max_queue_per_endpoint
        self._clock = clock
        self._condition = Condition()
        self._states: dict[EndpointIdentity, _EndpointState] = {}
        self._request_endpoint: dict[str, EndpointIdentity] = {}
        self._cancelled: set[str] = set()

    def _state(self, identity: EndpointIdentity) -> _EndpointState:
        return self._states.setdefault(identity, _EndpointState())

    def snapshot(self, identity: EndpointIdentity) -> EndpointQueueSnapshot:
        if not isinstance(identity, EndpointIdentity):
            raise TypeError("endpoint snapshot requires EndpointIdentity")
        with self._condition:
            state = self._state(identity)
            return EndpointQueueSnapshot(
                active_request_id=state.active_request_id,
                queued_request_ids=tuple(state.queue()),
                quarantined_reason=state.quarantined_reason,
            )

    def acquire(
        self,
        identity: EndpointIdentity,
        *,
        request_id: str,
        deadline_monotonic: float,
    ) -> EndpointLease:
        if not isinstance(identity, EndpointIdentity):
            raise TypeError("endpoint lease requires EndpointIdentity")
        request_id = _require_uuid("endpoint lease request_id", request_id)
        if not isinstance(deadline_monotonic, (int, float)) or isinstance(deadline_monotonic, bool):
            raise TypeError("endpoint lease deadline must be numeric monotonic time")
        deadline = float(deadline_monotonic)

        with self._condition:
            if request_id in self._request_endpoint:
                raise EndpointLeaseError("endpoint request identity is already active or queued")
            state = self._state(identity)
            if state.quarantined_reason is not None:
                raise EndpointQuarantined(state.quarantined_reason)

            queue = state.queue()
            if state.active_request_id is None and not queue:
                if deadline <= self._clock():
                    raise EndpointLeaseTimeout("endpoint lease deadline already expired")
                state.active_request_id = request_id
                self._request_endpoint[request_id] = identity
                return EndpointLease(self, identity, request_id)

            if len(queue) >= self.max_queue_per_endpoint:
                raise EndpointQueueFull("endpoint lease queue is full")
            if deadline <= self._clock():
                raise EndpointLeaseTimeout("endpoint lease deadline already expired")
            queue.append(request_id)
            self._request_endpoint[request_id] = identity

            try:
                while True:
                    if request_id in self._cancelled:
                        self._cancelled.remove(request_id)
                        raise EndpointRequestCancelled("queued endpoint request was cancelled")
                    if state.quarantined_reason is not None:
                        raise EndpointQuarantined(state.quarantined_reason)
                    if state.active_request_id is None and queue and queue[0] == request_id:
                        queue.popleft()
                        state.active_request_id = request_id
                        return EndpointLease(self, identity, request_id)

                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise EndpointLeaseTimeout("endpoint lease deadline expired while queued")
                    self._condition.wait(timeout=remaining)
            except BaseException:
                try:
                    queue.remove(request_id)
                except ValueError:
                    pass
                self._request_endpoint.pop(request_id, None)
                self._cancelled.discard(request_id)
                self._condition.notify_all()
                raise

    def cancel_queued(self, request_id: str) -> bool:
        request_id = _require_uuid("endpoint lease request_id", request_id)
        with self._condition:
            identity = self._request_endpoint.get(request_id)
            if identity is None:
                return False
            state = self._state(identity)
            if state.active_request_id == request_id:
                return False
            if request_id not in state.queue():
                return False
            self._cancelled.add(request_id)
            self._condition.notify_all()
            return True

    def quarantine(self, identity: EndpointIdentity, *, reason: str) -> None:
        if not isinstance(identity, EndpointIdentity):
            raise TypeError("endpoint quarantine requires EndpointIdentity")
        reason = _nonempty("quarantine reason", reason, limit=256)
        with self._condition:
            state = self._state(identity)
            state.quarantined_reason = reason
            self._condition.notify_all()

    def clear_quarantine(self, identity: EndpointIdentity) -> None:
        if not isinstance(identity, EndpointIdentity):
            raise TypeError("endpoint quarantine requires EndpointIdentity")
        with self._condition:
            self._state(identity).quarantined_reason = None
            self._condition.notify_all()

    def _release(self, identity: EndpointIdentity, request_id: str) -> None:
        with self._condition:
            state = self._state(identity)
            if state.active_request_id != request_id:
                raise EndpointLeaseError("endpoint lease release does not own the active slot")
            state.active_request_id = None
            self._request_endpoint.pop(request_id, None)
            self._condition.notify_all()


__all__ = [
    "EndpointIdentity",
    "EndpointLease",
    "EndpointLeaseError",
    "EndpointLeaseScheduler",
    "EndpointLeaseTimeout",
    "EndpointQueueFull",
    "EndpointQueueSnapshot",
    "EndpointQuarantined",
    "EndpointRequestCancelled",
]
