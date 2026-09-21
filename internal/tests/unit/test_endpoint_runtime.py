from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from local_agent.session.endpoint_lease import (
    EndpointArbiter,
    EndpointLeaseConflict,
    EndpointRequest,
    EndpointRole,
    EndpointUnavailable,
)
from local_agent.session.endpoint_runtime import EndpointAcquireTimeout, EndpointRuntime


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
