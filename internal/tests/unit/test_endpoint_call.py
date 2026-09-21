from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter, EndpointCallStateError
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRequest, EndpointRole, EndpointUnavailable
from local_agent.session.endpoint_runtime import EndpointRuntime


def _request(endpoint_id: str) -> EndpointRequest:
    return EndpointRequest(
        request_id=str(uuid4()),
        endpoint_id=endpoint_id,
        role=EndpointRole.WORKER,
        task_id=str(uuid4()),
        execution_epoch=0,
    )


def _adapter():
    endpoint_id = "ovms:npu:8000"
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    return endpoint_id, runtime, EndpointCallAdapter(runtime)


def test_successful_call_releases_endpoint_and_returns_wait_and_lease_identity():
    endpoint_id, runtime, adapter = _adapter()
    request = _request(endpoint_id)

    result = adapter.call(request, lambda left, right: left + right, 2, 3, timeout=0.1)

    assert result.value == 5
    assert result.wait_ms >= 0
    assert result.lease_id
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False


def test_client_exception_quarantines_exact_lease_until_explicit_reconciliation():
    endpoint_id, runtime, adapter = _adapter()
    handle = adapter.begin(_request(endpoint_id), timeout=0.1)

    def broken():
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        handle.invoke(broken)

    assert handle.state == "quarantined"
    assert runtime.arbiter.quarantined is True
    assert runtime.arbiter.active_lease is not None
    assert runtime.arbiter.active_lease.lease_id == handle.lease_id
    with pytest.raises(EndpointUnavailable):
        adapter.begin(_request(endpoint_id), timeout=0.1)

    assert handle.reconcile(known_stopped=False) is False
    assert handle.state == "quarantined"
    assert runtime.arbiter.quarantined is True
    assert handle.reconcile(known_stopped=True) is True
    assert handle.state == "reconciled"
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False


def test_endpoint_call_is_single_use_even_after_clean_completion():
    endpoint_id, _runtime, adapter = _adapter()
    handle = adapter.begin(_request(endpoint_id), timeout=0.1)
    first = handle.invoke(lambda: "ok")
    assert first.value == "ok"
    assert handle.state == "released"

    with pytest.raises(EndpointCallStateError, match="cannot invoke"):
        handle.invoke(lambda: "again")


def test_context_exception_before_invoke_releases_because_inference_never_started():
    endpoint_id, runtime, adapter = _adapter()
    handle = adapter.begin(_request(endpoint_id), timeout=0.1)

    with pytest.raises(ValueError, match="setup failed"):
        with handle:
            raise ValueError("setup failed")

    assert handle.state == "released"
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False


def test_release_without_call_is_allowed_once_only():
    endpoint_id, runtime, adapter = _adapter()
    handle = adapter.begin(_request(endpoint_id), timeout=0.1)
    handle.release_without_call()
    assert runtime.arbiter.active_lease is None

    with pytest.raises(EndpointCallStateError, match="cannot release unused"):
        handle.release_without_call()


def test_reconcile_before_quarantine_is_refused():
    endpoint_id, _runtime, adapter = _adapter()
    handle = adapter.begin(_request(endpoint_id), timeout=0.1)
    try:
        with pytest.raises(EndpointCallStateError, match="only a quarantined"):
            handle.reconcile(known_stopped=True)
    finally:
        handle.release_without_call()
