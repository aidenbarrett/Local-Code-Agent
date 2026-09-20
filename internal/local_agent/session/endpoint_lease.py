"""Deterministic in-process endpoint lease arbitration.

This is the policy core only. It serialises requests that name one physical endpoint
inside one process, enforces bounded queues, and models quarantine honestly. It is
not an OS-backed cross-process lock and does not claim that cancelling a client
future stopped the underlying inference request.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from uuid import UUID, uuid4


class EndpointLeaseError(RuntimeError):
    pass


class EndpointUnavailable(EndpointLeaseError):
    pass


class EndpointQueueFull(EndpointLeaseError):
    pass


class EndpointLeaseConflict(EndpointLeaseError):
    pass


class EndpointRole(str, Enum):
    CONVERSATION = "conversation"
    WORKER = "worker"
    STRONG = "strong"


class QueueClass(str, Enum):
    CHAT = "chat"
    WORK = "work"


@dataclass(frozen=True)
class EndpointRequest:
    request_id: str
    endpoint_id: str
    role: EndpointRole | str
    session_id: str | None = None
    task_id: str | None = None
    execution_epoch: int | None = None

    def __post_init__(self) -> None:
        try:
            UUID(self.request_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint request id must be a UUID") from exc
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id.strip():
            raise ValueError("endpoint id must be nonempty")
        object.__setattr__(self, "role", EndpointRole(self.role))

        if self.role == EndpointRole.CONVERSATION:
            if not isinstance(self.session_id, str) or not self.session_id.strip():
                raise ValueError("conversation endpoint request requires a session id")
            if self.task_id is not None or self.execution_epoch is not None:
                raise ValueError("conversation endpoint request cannot carry task execution authority")
        else:
            if self.session_id is not None and (
                not isinstance(self.session_id, str) or not self.session_id.strip()
            ):
                raise ValueError("worker session id must be nonempty when present")
            if self.task_id is None:
                raise ValueError("worker/strong endpoint request requires a task id")
            try:
                UUID(self.task_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("worker/strong endpoint task id must be a UUID") from exc
            if (
                not isinstance(self.execution_epoch, int)
                or isinstance(self.execution_epoch, bool)
                or self.execution_epoch < 0
            ):
                raise ValueError("worker/strong endpoint request requires a nonnegative execution epoch")

    @property
    def queue_class(self) -> QueueClass:
        if self.role == EndpointRole.CONVERSATION:
            return QueueClass.CHAT
        return QueueClass.WORK


@dataclass(frozen=True)
class EndpointLease:
    lease_id: str
    endpoint_id: str
    request: EndpointRequest

    def __post_init__(self) -> None:
        try:
            UUID(self.lease_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("endpoint lease id must be a UUID") from exc
        if self.endpoint_id != self.request.endpoint_id:
            raise ValueError("endpoint lease and request name different physical endpoints")


@dataclass(frozen=True)
class PendingPosition:
    queue_class: QueueClass
    class_position: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_class", QueueClass(self.queue_class))
        if (
            not isinstance(self.class_position, int)
            or isinstance(self.class_position, bool)
            or self.class_position < 0
        ):
            raise ValueError("class queue position must be a nonnegative integer")


class EndpointArbiter:
    """One in-process arbiter for one physical endpoint identity."""

    def __init__(
        self,
        endpoint_id: str,
        *,
        chat_pending_limit_per_session: int = 4,
        work_pending_limit: int = 64,
    ):
        if not isinstance(endpoint_id, str) or not endpoint_id.strip():
            raise ValueError("endpoint id must be nonempty")
        for name, value in (
            ("chat_pending_limit_per_session", chat_pending_limit_per_session),
            ("work_pending_limit", work_pending_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.endpoint_id = endpoint_id
        self.chat_pending_limit_per_session = chat_pending_limit_per_session
        self.work_pending_limit = work_pending_limit
        self._lock = Lock()
        self._chat: deque[EndpointRequest] = deque()
        self._work: deque[EndpointRequest] = deque()
        self._active: EndpointLease | None = None
        self._quarantine_reason: str | None = None
        self._next_class = QueueClass.CHAT

    @property
    def active_lease(self) -> EndpointLease | None:
        with self._lock:
            return self._active

    @property
    def quarantine_reason(self) -> str | None:
        with self._lock:
            return self._quarantine_reason

    @property
    def quarantined(self) -> bool:
        with self._lock:
            return self._quarantine_reason is not None

    def _all_request_ids(self) -> set[str]:
        ids = {request.request_id for request in self._chat}
        ids.update(request.request_id for request in self._work)
        if self._active is not None:
            ids.add(self._active.request.request_id)
        return ids

    def enqueue(self, request: EndpointRequest) -> PendingPosition:
        if not isinstance(request, EndpointRequest):
            raise TypeError("endpoint queue accepts EndpointRequest values")
        if request.endpoint_id != self.endpoint_id:
            raise ValueError("request belongs to a different physical endpoint")
        with self._lock:
            if self._quarantine_reason is not None:
                raise EndpointUnavailable(
                    f"endpoint is quarantined: {self._quarantine_reason}"
                )
            if request.request_id in self._all_request_ids():
                raise EndpointLeaseConflict("endpoint request id is already active or queued")

            if request.queue_class == QueueClass.CHAT:
                assert request.session_id is not None
                pending_for_session = sum(
                    1 for item in self._chat if item.session_id == request.session_id
                )
                if pending_for_session >= self.chat_pending_limit_per_session:
                    raise EndpointQueueFull("conversation endpoint queue is full for this session")
                self._chat.append(request)
                return PendingPosition(QueueClass.CHAT, pending_for_session)

            if len(self._work) >= self.work_pending_limit:
                raise EndpointQueueFull("worker endpoint queue is full")
            position = len(self._work)
            self._work.append(request)
            return PendingPosition(QueueClass.WORK, position)

    def _pick_queue(self) -> deque[EndpointRequest] | None:
        preferred = self._chat if self._next_class == QueueClass.CHAT else self._work
        alternate = self._work if self._next_class == QueueClass.CHAT else self._chat
        if preferred:
            return preferred
        if alternate:
            return alternate
        return None

    def acquire_next(self) -> EndpointLease | None:
        """Acquire at most one request; never queue work inside the endpoint server."""
        with self._lock:
            if self._quarantine_reason is not None:
                raise EndpointUnavailable(
                    f"endpoint is quarantined: {self._quarantine_reason}"
                )
            if self._active is not None:
                return None
            queue = self._pick_queue()
            if queue is None:
                return None
            request = queue.popleft()
            lease = EndpointLease(str(uuid4()), self.endpoint_id, request)
            self._active = lease
            return lease

    def _finish_active(self) -> EndpointLease:
        if self._active is None:
            raise EndpointLeaseConflict("endpoint has no active lease")
        released = self._active
        self._active = None
        self._next_class = (
            QueueClass.WORK
            if released.request.queue_class == QueueClass.CHAT
            else QueueClass.CHAT
        )
        return released

    def release(self, lease_id: str) -> EndpointLease:
        """Release a known-clean active lease.

        A quarantined lease cannot use this path; it must be explicitly reconciled
        so quarantine is never cleared by an ordinary success-shaped release.
        """
        try:
            UUID(lease_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("lease id must be a UUID") from exc
        with self._lock:
            if self._active is None or self._active.lease_id != lease_id:
                raise EndpointLeaseConflict("lease id is not the active endpoint lease")
            if self._quarantine_reason is not None:
                raise EndpointLeaseConflict("quarantined endpoint requires explicit reconciliation")
            return self._finish_active()

    def quarantine(self, reason: str, *, lease_id: str | None = None) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("endpoint quarantine reason must be nonempty")
        if len(reason) > 256:
            raise ValueError("endpoint quarantine reason exceeds size limit")
        if lease_id is not None:
            try:
                UUID(lease_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("lease id must be a UUID") from exc
        with self._lock:
            if lease_id is not None and (
                self._active is None or self._active.lease_id != lease_id
            ):
                raise EndpointLeaseConflict("cannot quarantine a non-active lease")
            if self._quarantine_reason is not None and self._quarantine_reason != reason:
                raise EndpointLeaseConflict("endpoint is already quarantined for a different reason")
            self._quarantine_reason = reason

    def reconcile_quarantine(
        self,
        *,
        known_stopped: bool,
        lease_id: str | None = None,
    ) -> bool:
        """Clear quarantine only after the uncertain active request is known stopped."""
        if not isinstance(known_stopped, bool):
            raise TypeError("known_stopped must be boolean")
        if lease_id is not None:
            try:
                UUID(lease_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("lease id must be a UUID") from exc
        with self._lock:
            if self._quarantine_reason is None:
                raise EndpointLeaseConflict("endpoint is not quarantined")
            if lease_id is not None and (
                self._active is None or self._active.lease_id != lease_id
            ):
                raise EndpointLeaseConflict("reconciliation lease is not active")
            if not known_stopped:
                return False
            if self._active is not None:
                self._finish_active()
            self._quarantine_reason = None
            return True

    def remove_queued(self, request_id: str) -> EndpointRequest | None:
        """Atomically remove queued work; the active lease is never removed here."""
        try:
            UUID(request_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("request id must be a UUID") from exc
        with self._lock:
            for queue in (self._chat, self._work):
                for index, request in enumerate(queue):
                    if request.request_id == request_id:
                        del queue[index]
                        return request
            return None

    def pending_position(self, request_id: str) -> PendingPosition | None:
        """Return class-local FIFO position only; no ETA or cross-class prediction."""
        try:
            UUID(request_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("request id must be a UUID") from exc
        with self._lock:
            for index, request in enumerate(self._chat):
                if request.request_id == request_id:
                    assert request.session_id is not None
                    position = sum(
                        1
                        for prior in list(self._chat)[:index]
                        if prior.session_id == request.session_id
                    )
                    return PendingPosition(QueueClass.CHAT, position)
            for index, request in enumerate(self._work):
                if request.request_id == request_id:
                    return PendingPosition(QueueClass.WORK, index)
            return None
