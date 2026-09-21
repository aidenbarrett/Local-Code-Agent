from __future__ import annotations

import threading
import time
from uuid import uuid4

import pytest

from local_agent.session.endpoint_lease import (
    EndpointArbiter,
    EndpointLeaseConflict,
    EndpointRequest,
    EndpointRole,
    EndpointUnavailable,
)
from local_agent.session.endpoint_runtime import (
    EndpointAcquireTimeout,
    EndpointRequestCancelled,
    EndpointRuntime,
)


def _conversation_request(endpoint_id: str, session_id: str = "session-a") -> EndpointRequest:
    return EndpointRequest(
        request_id=str(uuid4()),
        endpoint_id=endpoint_id,
        role=EndpointRole.CONVERSATION,
        session_id=session_id,
    )


def _work_request(endpoint_id: str) -> EndpointRequest:
    return EndpointRequest(
        request_id=str(uuid4()),
        endpoint_id=endpoint_id,
        role=EndpointRole.WORKER,
        task_id=str(uuid4()),
        execution_epoch=0,
    )


def _wait_until_queued(runtime: EndpointRuntime, request_id: str) -> None:
    deadline = time.monotonic() + 1
    while runtime.arbiter.pending_position(request_id) is None:
        if time.monotonic() >= deadline:
            raise AssertionError("endpoint request did not enter the queue")
        time.sleep(0.005)


def test_clean_context_release_allows_the_next_request_to_acquire():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)
    assert runtime.arbiter.active_lease is not None

    first.release()
    second = runtime.acquire(_work_request(endpoint_id), timeout=0.1)
    try:
        assert second.request.role is EndpointRole.WORKER
        assert runtime.arbiter.active_lease.lease_id == second.lease_id
    finally:
        second.release()
    assert runtime.arbiter.active_lease is None


def test_waiter_blocks_until_active_lease_releases_then_gets_exact_request():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)
    request = _work_request(endpoint_id)
    entered = threading.Event()
    acquired = threading.Event()
    box = {}
    errors = []

    def wait_for_lease():
        entered.set()
        try:
            lease = runtime.acquire(request, timeout=2)
            box["lease"] = lease
            acquired.set()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=wait_for_lease)
    thread.start()
    assert entered.wait(1)
    assert acquired.wait(0.05) is False

    first.release()
    assert acquired.wait(1)
    thread.join(1)
    assert not errors
    assert not thread.is_alive()
    second = box["lease"]
    assert second.request == request
    second.release()


def test_timeout_removes_queued_request_without_leaving_a_ghost_lease():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)
    queued = _work_request(endpoint_id)

    with pytest.raises(EndpointAcquireTimeout):
        runtime.acquire(queued, timeout=0)
    assert runtime.arbiter.pending_position(queued.request_id) is None
    assert runtime.arbiter.active_lease.lease_id == first.lease_id

    first.release()
    assert runtime.arbiter.active_lease is None


def test_queued_cancellation_wakes_exact_unbounded_waiter_without_dispatch():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)
    queued = _work_request(endpoint_id)
    box = {}

    def waiter():
        try:
            runtime.acquire(queued, timeout=None)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_until_queued(runtime, queued.request_id)

    removed = runtime.cancel_queued(queued.request_id)
    thread.join(1)

    assert removed == queued
    assert isinstance(box.get("error"), EndpointRequestCancelled)
    assert not thread.is_alive()
    assert runtime.arbiter.pending_position(queued.request_id) is None
    assert runtime.arbiter.active_lease.lease_id == first.lease_id
    first.release()
    assert runtime.arbiter.active_lease is None


def test_queued_cancellation_is_exact_and_does_not_remove_other_waiters():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)
    cancelled = _work_request(endpoint_id)
    survivor = _work_request(endpoint_id)
    errors = {}
    leases = {}

    def wait_cancelled():
        try:
            runtime.acquire(cancelled, timeout=None)
        except BaseException as exc:
            errors["cancelled"] = exc

    def wait_survivor():
        try:
            leases["survivor"] = runtime.acquire(survivor, timeout=2)
        except BaseException as exc:
            errors["survivor"] = exc

    first_thread = threading.Thread(target=wait_cancelled)
    second_thread = threading.Thread(target=wait_survivor)
    first_thread.start()
    _wait_until_queued(runtime, cancelled.request_id)
    second_thread.start()
    _wait_until_queued(runtime, survivor.request_id)

    assert runtime.cancel_queued(cancelled.request_id) == cancelled
    first_thread.join(1)
    assert isinstance(errors.get("cancelled"), EndpointRequestCancelled)
    assert runtime.arbiter.pending_position(survivor.request_id) is not None

    first.release()
    second_thread.join(1)
    assert "survivor" not in errors
    assert leases["survivor"].request == survivor
    leases["survivor"].release()


def test_granted_request_cannot_be_relabelled_as_queued_cancellation():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    lease = runtime.acquire(_work_request(endpoint_id), timeout=0.1)

    with pytest.raises(EndpointLeaseConflict, match="after it was granted"):
        runtime.cancel_queued(lease.request.request_id)
    assert runtime.arbiter.active_lease.lease_id == lease.lease_id
    lease.release()


def test_exception_in_lease_scope_quarantines_instead_of_releasing():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    lease = runtime.acquire(_work_request(endpoint_id), timeout=0.1)

    with pytest.raises(RuntimeError, match="boom"):
        with lease:
            raise RuntimeError("boom")

    assert lease.state == "quarantined"
    assert runtime.arbiter.quarantined is True
    assert runtime.arbiter.active_lease.lease_id == lease.lease_id
    with pytest.raises(EndpointUnavailable):
        runtime.acquire(_conversation_request(endpoint_id), timeout=0.1)

    assert lease.reconcile(known_stopped=False) is False
    assert lease.state == "quarantined"
    assert runtime.arbiter.quarantined is True
    assert lease.reconcile(known_stopped=True) is True
    assert lease.state == "reconciled"
    assert runtime.arbiter.quarantined is False
    assert runtime.arbiter.active_lease is None


def test_explicit_quarantine_keeps_waiting_work_fenced_until_reconciled():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    first = runtime.acquire(_work_request(endpoint_id), timeout=0.1)
    waiting_request = _conversation_request(endpoint_id)
    entered = threading.Event()
    acquired = threading.Event()
    box = {}

    def waiter():
        entered.set()
        box["lease"] = runtime.acquire(waiting_request, timeout=2)
        acquired.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert entered.wait(1)
    assert acquired.wait(0.05) is False

    first.quarantine("underlying request stop is unconfirmed")
    assert acquired.wait(0.05) is False
    assert first.reconcile(known_stopped=False) is False
    assert acquired.wait(0.05) is False

    assert first.reconcile(known_stopped=True) is True
    assert acquired.wait(1)
    thread.join(1)
    second = box["lease"]
    assert second.request == waiting_request
    second.release()


def test_managed_lease_cannot_be_released_after_it_is_quarantined():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    lease = runtime.acquire(_work_request(endpoint_id), timeout=0.1)
    lease.quarantine("uncertain stop")

    with pytest.raises(EndpointLeaseConflict, match="cannot release"):
        lease.release()
    assert lease.reconcile(known_stopped=True) is True