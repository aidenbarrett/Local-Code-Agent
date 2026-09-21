"""Call boundary over the managed endpoint lease runtime.

Acquiring a physical endpoint and invoking a client call must be one ownership story.
This adapter gives callers an explicit handle: success releases the lease; any exception
raised by the client quarantines the exact lease because client-side failure is not proof
that the underlying inference stopped. Reconciliation remains explicit afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from .endpoint_lease import EndpointLeaseConflict, EndpointRequest
from .endpoint_runtime import EndpointRuntime, ManagedEndpointLease


class EndpointCallStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class EndpointCallResult:
    value: Any
    wait_ms: int
    lease_id: str


class ManagedEndpointCall:
    """One acquired endpoint lease and at most one client invocation."""

    _CALL_EXCEPTION_REASON = "client call exited without proof underlying inference stopped"

    def __init__(self, lease: ManagedEndpointLease):
        self.lease = lease
        self._lock = Lock()
        self._state = "ready"

    @property
    def request(self) -> EndpointRequest:
        return self.lease.request

    @property
    def lease_id(self) -> str:
        return self.lease.lease_id

    @property
    def wait_ms(self) -> int:
        return self.lease.wait_ms

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def invoke(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> EndpointCallResult:
        if not callable(fn):
            raise TypeError("endpoint call target must be callable")
        with self._lock:
            if self._state != "ready":
                raise EndpointCallStateError(
                    f"endpoint call cannot invoke from state {self._state!r}"
                )
            self._state = "invoking"
        try:
            value = fn(*args, **kwargs)
        except BaseException:
            try:
                self.lease.quarantine(self._CALL_EXCEPTION_REASON)
            finally:
                with self._lock:
                    self._state = "quarantined"
            raise

        try:
            self.lease.release()
        except BaseException:
            with self._lock:
                self._state = "release_failed"
            raise
        with self._lock:
            self._state = "released"
        return EndpointCallResult(value=value, wait_ms=self.wait_ms, lease_id=self.lease_id)

    def release_without_call(self) -> None:
        """Release ownership only when no inference call was ever started."""
        with self._lock:
            if self._state != "ready":
                raise EndpointCallStateError(
                    f"endpoint call cannot release unused lease from state {self._state!r}"
                )
            self._state = "releasing"
        try:
            self.lease.release()
        except BaseException:
            with self._lock:
                self._state = "release_failed"
            raise
        with self._lock:
            self._state = "released"

    def reconcile(self, *, known_stopped: bool) -> bool:
        with self._lock:
            if self._state != "quarantined":
                raise EndpointCallStateError("only a quarantined endpoint call can reconcile")
        cleared = self.lease.reconcile(known_stopped=known_stopped)
        if cleared:
            with self._lock:
                self._state = "reconciled"
        return cleared

    def __enter__(self) -> "ManagedEndpointCall":
        if self.state != "ready":
            raise EndpointCallStateError("endpoint call handle is not ready")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # An exception before invoke() began did not start inference, so the endpoint
        # can be released. invoke() itself changes state before executing and handles
        # abnormal completion by quarantining.
        if self.state == "ready":
            self.release_without_call()
        return False


class EndpointCallAdapter:
    """Acquire a deterministic lease and hand back an explicit call handle."""

    def __init__(self, runtime: EndpointRuntime):
        if not isinstance(runtime, EndpointRuntime):
            raise TypeError("endpoint call adapter requires EndpointRuntime")
        self.runtime = runtime

    def begin(
        self,
        request: EndpointRequest,
        *,
        timeout: float | None = None,
    ) -> ManagedEndpointCall:
        lease = self.runtime.acquire(request, timeout=timeout)
        return ManagedEndpointCall(lease)

    def call(
        self,
        request: EndpointRequest,
        fn: Callable[..., Any],
        /,
        *args: Any,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> EndpointCallResult:
        """Convenience path when the caller does not need later reconciliation access.

        Use ``begin`` when a quarantined failure must be explicitly reconciled by the
        caller; this convenience method intentionally propagates client exceptions.
        """
        handle = self.begin(request, timeout=timeout)
        return handle.invoke(fn, *args, **kwargs)
