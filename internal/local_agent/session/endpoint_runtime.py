"""Managed in-process lifecycle for one physical inference endpoint.

``endpoint_lease.py`` owns deterministic queue/fairness policy. This module adds the
blocking runtime boundary callers actually need: enqueue, wait for the exact grant,
release cleanly, or quarantine on uncertain completion. An exception leaving a lease
scope is not proof that inference stopped, so abnormal exit quarantines instead of
silently releasing the endpoint to another request.

This remains deliberately in-process. It is not an OS-backed cross-process lock and it
does not claim to cancel an inference server request.
"""
from __future__ import annotations

from types import TracebackType
from threading import Condition, Lock
from time import monotonic

from .endpoint_lease import (
    EndpointArbiter,
    EndpointLease,
    EndpointLeaseConflict,
    EndpointLeaseError,
    EndpointRequest,
    EndpointUnavailable,
)


class EndpointAcquireTimeout(EndpointLeaseError):
    """A queued request did not acquire its endpoint within the caller's bound."""


class EndpointRequestCancelled(EndpointLeaseError):
    """The exact queued endpoint request was atomically removed before dispatch."""


class EndpointRuntime:
    """Blocking lifecycle wrapper around one deterministic ``EndpointArbiter``."""

    def __init__(self, arbiter: EndpointArbiter):
        if not isinstance(arbiter, EndpointArbiter):
            raise TypeError("endpoint runtime requires an EndpointArbiter")
        self.arbiter = arbiter
        self._condition = Condition()
        self._granted: dict[str, EndpointLease] = {}
        self._cancelled: set[str] = set()
        self._cancelled_executions: set[tuple[str, int]] = set()

    @property
    def endpoint_id(self) -> str:
        return self.arbiter.endpoint_id

    def _pump_locked(self) -> None:
        """Grant at most one queued request while holding the runtime condition."""
        if self.arbiter.active_lease is not None or self.arbiter.quarantined:
            return
        lease = self.arbiter.acquire_next()
        if lease is None:
            return
        request_id = lease.request.request_id
        if request_id in self._granted:
            raise EndpointLeaseConflict("endpoint request received two runtime grants")
        self._granted[request_id] = lease
        self._condition.notify_all()

    def acquire(
        self,
        request: EndpointRequest,
        *,
        timeout: float | None = None,
    ) -> "ManagedEndpointLease":
        """Enqueue and wait until this exact request owns the physical endpoint.

        Timeout removes only a still-queued request. Because grant/release/pumping all
        happen under the same condition, timeout cannot race a hidden grant into a ghost
        active lease. Explicit queued cancellation uses the same condition and wakes the
        exact waiter with ``EndpointRequestCancelled`` rather than leaving it blocked.
        """
        if not isinstance(request, EndpointRequest):
            raise TypeError("endpoint runtime accepts EndpointRequest values")
        if request.endpoint_id != self.endpoint_id:
            raise ValueError("request belongs to a different physical endpoint")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
                raise ValueError("endpoint acquire timeout must be non-negative or None")
            timeout = float(timeout)

        started = monotonic()
        with self._condition:
            if (
                request.task_id is not None
                and request.execution_epoch is not None
                and (request.task_id, request.execution_epoch) in self._cancelled_executions
            ):
                raise EndpointRequestCancelled(
                    "task execution was cancelled before endpoint dispatch"
                )
            self.arbiter.enqueue(request)
            self._pump_locked()
            while True:
                granted = self._granted.pop(request.request_id, None)
                if granted is not None:
                    wait_ms = max(0, int((monotonic() - started) * 1000))
                    return ManagedEndpointLease(self, granted, wait_ms=wait_ms)

                if request.request_id in self._cancelled:
                    self._cancelled.remove(request.request_id)
                    raise EndpointRequestCancelled(
                        f"endpoint request {request.request_id!r} was cancelled before dispatch"
                    )

                if timeout is None:
                    self._condition.wait()
                    continue

                remaining = timeout - (monotonic() - started)
                if remaining <= 0:
                    removed = self.arbiter.remove_queued(request.request_id)
                    if removed is None:
                        # No other runtime transition can run while this condition is
                        # held. Missing queued state therefore signals external/broken
                        # arbiter ownership; fail closed rather than forgetting a grant.
                        raise EndpointLeaseConflict(
                            "timed-out endpoint request is neither granted nor queued"
                        )
                    raise EndpointAcquireTimeout(
                        f"endpoint request did not acquire {self.endpoint_id!r} before timeout"
                    )
                self._condition.wait(remaining)

    def cancel_queued(self, request_id: str) -> EndpointRequest | None:
        """Atomically remove one still-queued request and wake its exact waiter.

        This operation deliberately refuses to reinterpret an already-granted request
        as a clean queued cancellation. Once a lease exists, cancellation must use the
        active-call quarantine/reconciliation path because Python-side cancellation is
        not proof that inference stopped.
        """
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("endpoint request id must be nonempty")
        with self._condition:
            granted = self._granted.get(request_id)
            active = self.arbiter.active_lease
            if granted is not None or (
                active is not None and active.request.request_id == request_id
            ):
                raise EndpointLeaseConflict(
                    "cannot cancel endpoint request as queued after it was granted"
                )
            removed = self.arbiter.remove_queued(request_id)
            if removed is None:
                return None
            self._cancelled.add(request_id)
            self._pump_locked()
            self._condition.notify_all()
            return removed

    def cancel_execution(self, task_id: str, execution_epoch: int) -> tuple[str, ...]:
        """Fence endpoint work owned by one exact task execution.

        Still-queued requests are removed and their waiters are woken. A grant that has
        not yet escaped acquire is released because no caller can have started
        inference. An already handed-out active lease is quarantined instead: fencing
        reuse is safe, but this method does not claim the underlying inference stopped.

        Returns request IDs removed before dispatch. An empty tuple with a matching
        active lease therefore means quarantine, not successful endpoint cancellation.
        """
        with self._condition:
            self._cancelled_executions.add((task_id, execution_epoch))
            active = self.arbiter.active_lease
            if (
                active is not None
                and active.request.task_id == task_id
                and active.request.execution_epoch == execution_epoch
            ):
                pending_grant = self._granted.pop(active.request.request_id, None)
                if pending_grant is not None:
                    self.arbiter.release(active.lease_id)
                    self._cancelled.add(active.request.request_id)
                    self._pump_locked()
                    self._condition.notify_all()
                    return (active.request.request_id,)
                self.arbiter.quarantine(
                    "task execution stopped without proof underlying inference stopped",
                    lease_id=active.lease_id,
                )
                self._condition.notify_all()
                return ()

            removed = self.arbiter.remove_queued_execution(task_id, execution_epoch)
            for request in removed:
                self._cancelled.add(request.request_id)
            if removed:
                self._pump_locked()
                self._condition.notify_all()
            return tuple(request.request_id for request in removed)

    def complete_call(self, lease_id: str) -> tuple[EndpointLease, bool]:
        """Finish a synchronous call using its normal return as stop proof.

        Stop may quarantine a handed-out lease while its client call is still running.
        If that exact synchronous call later returns normally, the call itself is no
        longer in flight. Reconcile only that exact quarantined lease; otherwise use the
        ordinary clean-release path. The boolean reports whether quarantine was cleared.
        """
        with self._condition:
            active = self.arbiter.active_lease
            if active is None or active.lease_id != lease_id:
                raise EndpointLeaseConflict("completed call is not the active endpoint lease")
            reconciled = self.arbiter.quarantined
            if reconciled:
                cleared = self.arbiter.reconcile_quarantine(
                    known_stopped=True,
                    lease_id=lease_id,
                )
                if not cleared:
                    raise EndpointLeaseConflict("completed call could not reconcile quarantine")
                released = active
            else:
                released = self.arbiter.release(lease_id)
            self._pump_locked()
            self._condition.notify_all()
            return released, reconciled

    def release(self, lease_id: str) -> EndpointLease:
        """Release a known-clean active lease, then grant the next eligible request."""
        with self._condition:
            released = self.arbiter.release(lease_id)
            self._pump_locked()
            self._condition.notify_all()
            return released

    def owns_quarantined_lease(self, lease_id: str) -> bool:
        """Report whether Stop already fenced this exact active lease."""
        with self._condition:
            active = self.arbiter.active_lease
            return bool(
                self.arbiter.quarantined
                and active is not None
                and active.lease_id == lease_id
            )

    def quarantine(self, lease_id: str, reason: str) -> None:
        """Fence an uncertain active request without freeing the physical endpoint."""
        with self._condition:
            self.arbiter.quarantine(reason, lease_id=lease_id)
            self._condition.notify_all()

    def reconcile_quarantine(self, lease_id: str, *, known_stopped: bool) -> bool:
        """Clear quarantine only when the exact active request is proved stopped."""
        with self._condition:
            cleared = self.arbiter.reconcile_quarantine(
                known_stopped=known_stopped,
                lease_id=lease_id,
            )
            if cleared:
                self._pump_locked()
            self._condition.notify_all()
            return cleared


class ManagedEndpointLease:
    """Context-managed ownership token for one granted endpoint request."""

    _ABNORMAL_EXIT_REASON = "lease body exited before clean endpoint release"

    def __init__(self, runtime: EndpointRuntime, lease: EndpointLease, *, wait_ms: int):
        self.runtime = runtime
        self.lease = lease
        self.wait_ms = wait_ms
        self._lock = Lock()
        self._state = "open"

    @property
    def lease_id(self) -> str:
        return self.lease.lease_id

    @property
    def request(self) -> EndpointRequest:
        return self.lease.request

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def complete_call(self) -> EndpointLease:
        """Finish a normally-returned client call, reconciling an exact Stop fence."""
        with self._lock:
            if self._state != "open":
                raise EndpointLeaseConflict(
                    f"managed endpoint lease cannot complete from state {self._state!r}"
                )
            released, reconciled = self.runtime.complete_call(self.lease_id)
            self._state = "reconciled" if reconciled else "released"
            return released

    def release(self) -> EndpointLease:
        with self._lock:
            if self._state != "open":
                raise EndpointLeaseConflict(
                    f"managed endpoint lease cannot release from state {self._state!r}"
                )
            released = self.runtime.release(self.lease_id)
            self._state = "released"
            return released

    def adopt_existing_quarantine(self) -> bool:
        """Join a quarantine established externally for this exact active lease."""
        with self._lock:
            if self._state != "open":
                return self._state == "quarantined"
            if not self.runtime.owns_quarantined_lease(self.lease_id):
                return False
            self._state = "quarantined"
            return True

    def quarantine(self, reason: str) -> None:
        with self._lock:
            if self._state != "open":
                raise EndpointLeaseConflict(
                    f"managed endpoint lease cannot quarantine from state {self._state!r}"
                )
            self.runtime.quarantine(self.lease_id, reason)
            self._state = "quarantined"

    def reconcile(self, *, known_stopped: bool) -> bool:
        with self._lock:
            if self._state != "quarantined":
                raise EndpointLeaseConflict("only a quarantined managed lease can reconcile")
            cleared = self.runtime.reconcile_quarantine(
                self.lease_id,
                known_stopped=known_stopped,
            )
            if cleared:
                self._state = "reconciled"
            return cleared

    def __enter__(self) -> "ManagedEndpointLease":
        if self.state != "open":
            raise EndpointLeaseConflict("managed endpoint lease is not open")
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        state = self.state
        if state != "open":
            return False
        if exc_type is None:
            self.release()
        else:
            # Do not persist arbitrary exception prose as endpoint policy. The fixed
            # reason states exactly what is known: caller scope ended without proof of
            # a clean underlying inference stop.
            self.quarantine(self._ABNORMAL_EXIT_REASON)
        return False