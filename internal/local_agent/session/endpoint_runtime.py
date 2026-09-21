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


class EndpointRuntime:
    """Blocking lifecycle wrapper around one deterministic ``EndpointArbiter``."""

    def __init__(self, arbiter: EndpointArbiter):
        if not isinstance(arbiter, EndpointArbiter):
            raise TypeError("endpoint runtime requires an EndpointArbiter")
        self.arbiter = arbiter
        self._condition = Condition()
        self._granted: dict[str, EndpointLease] = {}

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
        active lease.
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
            self.arbiter.enqueue(request)
            self._pump_locked()
            while True:
                granted = self._granted.pop(request.request_id, None)
                if granted is not None:
                    wait_ms = max(0, int((monotonic() - started) * 1000))
                    return ManagedEndpointLease(self, granted, wait_ms=wait_ms)

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

    def release(self, lease_id: str) -> EndpointLease:
        """Release a known-clean active lease, then grant the next eligible request."""
        with self._condition:
            released = self.arbiter.release(lease_id)
            self._pump_locked()
            self._condition.notify_all()
            return released

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

    def release(self) -> EndpointLease:
        with self._lock:
            if self._state != "open":
                raise EndpointLeaseConflict(
                    f"managed endpoint lease cannot release from state {self._state!r}"
                )
            released = self.runtime.release(self.lease_id)
            self._state = "released"
            return released

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

    def __exit__(self, exc_type, exc, tb) -> bool:
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
