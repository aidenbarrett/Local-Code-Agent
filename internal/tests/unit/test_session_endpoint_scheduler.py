from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest

from local_agent.session.scheduler import (
    EndpointIdentity,
    EndpointLeaseScheduler,
    EndpointLeaseTimeout,
    EndpointQueueFull,
    EndpointQuarantined,
    EndpointRequestCancelled,
)


def _identity(url: str = "HTTP://LOCALHOST:8000/v3/") -> EndpointIdentity:
    return EndpointIdentity(url, "Qwen3-8B", "rev-1", "NPU")


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for endpoint scheduler state")
        time.sleep(0.005)


def test_endpoint_identity_normalises_physical_endpoint_not_profile_name():
    one = _identity()
    two = EndpointIdentity("http://localhost:8000/v3", "Qwen3-8B", "rev-1", "npu")
    assert one == two
    assert one.base_url == "http://localhost:8000/v3"
    assert one.device == "npu"
    with pytest.raises(ValueError, match="credentials"):
        EndpointIdentity("http://user:secret@localhost:8000", "m", "r", "cpu")


def test_one_active_lease_and_fifo_waiters():
    scheduler = EndpointLeaseScheduler(max_queue_per_endpoint=3)
    identity = _identity()
    first = str(uuid4())
    second = str(uuid4())
    third = str(uuid4())
    lease = scheduler.acquire(identity, request_id=first, deadline_monotonic=time.monotonic() + 3)
    order: list[str] = []
    errors: list[BaseException] = []

    def waiter(request_id: str) -> None:
        try:
            with scheduler.acquire(
                identity,
                request_id=request_id,
                deadline_monotonic=time.monotonic() + 3,
            ):
                order.append(request_id)
                time.sleep(0.02)
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    t2 = threading.Thread(target=waiter, args=(second,))
    t3 = threading.Thread(target=waiter, args=(third,))
    t2.start()
    _wait_for(lambda: scheduler.snapshot(identity).queued_request_ids == (second,))
    t3.start()
    _wait_for(lambda: scheduler.snapshot(identity).queued_request_ids == (second, third))
    lease.release()
    t2.join(2)
    t3.join(2)

    assert errors == []
    assert order == [second, third]
    assert scheduler.snapshot(identity).active_request_id is None


def test_bounded_queue_refuses_extra_request():
    scheduler = EndpointLeaseScheduler(max_queue_per_endpoint=1)
    identity = _identity()
    active = scheduler.acquire(identity, request_id=str(uuid4()), deadline_monotonic=time.monotonic() + 3)
    queued = str(uuid4())
    release = threading.Event()

    def waiter() -> None:
        with scheduler.acquire(identity, request_id=queued, deadline_monotonic=time.monotonic() + 3):
            release.wait(1)

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_for(lambda: scheduler.snapshot(identity).queued_request_ids == (queued,))
    with pytest.raises(EndpointQueueFull):
        scheduler.acquire(identity, request_id=str(uuid4()), deadline_monotonic=time.monotonic() + 3)
    active.release()
    _wait_for(lambda: scheduler.snapshot(identity).active_request_id == queued)
    release.set()
    thread.join(2)


def test_queued_cancellation_is_explicit_and_never_preempts_active_lease():
    scheduler = EndpointLeaseScheduler(max_queue_per_endpoint=2)
    identity = _identity()
    active_id = str(uuid4())
    queued = str(uuid4())
    active = scheduler.acquire(identity, request_id=active_id, deadline_monotonic=time.monotonic() + 3)
    observed: list[type[BaseException]] = []

    def waiter() -> None:
        try:
            scheduler.acquire(identity, request_id=queued, deadline_monotonic=time.monotonic() + 3)
        except BaseException as exc:
            observed.append(type(exc))

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_for(lambda: scheduler.snapshot(identity).queued_request_ids == (queued,))
    assert scheduler.cancel_queued(active_id) is False
    assert scheduler.cancel_queued(queued) is True
    thread.join(2)
    assert observed == [EndpointRequestCancelled]
    assert scheduler.snapshot(identity).active_request_id == active_id
    active.release()


def test_queue_deadline_removes_waiter_without_ghost_ownership():
    scheduler = EndpointLeaseScheduler(max_queue_per_endpoint=2)
    identity = _identity()
    active = scheduler.acquire(identity, request_id=str(uuid4()), deadline_monotonic=time.monotonic() + 3)
    queued = str(uuid4())
    observed: list[type[BaseException]] = []

    def waiter() -> None:
        try:
            scheduler.acquire(identity, request_id=queued, deadline_monotonic=time.monotonic() + 0.05)
        except BaseException as exc:
            observed.append(type(exc))

    thread = threading.Thread(target=waiter)
    thread.start()
    thread.join(1)
    assert observed == [EndpointLeaseTimeout]
    assert scheduler.snapshot(identity).queued_request_ids == ()
    active.release()


def test_quarantine_rejects_waiters_and_future_acquires_until_cleared():
    scheduler = EndpointLeaseScheduler(max_queue_per_endpoint=2)
    identity = _identity()
    active_id = str(uuid4())
    active = scheduler.acquire(identity, request_id=active_id, deadline_monotonic=time.monotonic() + 3)
    queued = str(uuid4())
    observed: list[str] = []

    def waiter() -> None:
        try:
            scheduler.acquire(identity, request_id=queued, deadline_monotonic=time.monotonic() + 3)
        except EndpointQuarantined as exc:
            observed.append(str(exc))

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_for(lambda: scheduler.snapshot(identity).queued_request_ids == (queued,))
    scheduler.quarantine(identity, reason="runtime cancellation unresolved")
    thread.join(2)
    assert observed == ["runtime cancellation unresolved"]
    assert scheduler.snapshot(identity).active_request_id == active_id
    with pytest.raises(EndpointQuarantined, match="runtime cancellation unresolved"):
        scheduler.acquire(identity, request_id=str(uuid4()), deadline_monotonic=time.monotonic() + 3)

    active.release()
    scheduler.clear_quarantine(identity)
    with scheduler.acquire(identity, request_id=str(uuid4()), deadline_monotonic=time.monotonic() + 3):
        assert scheduler.snapshot(identity).active_request_id is not None
