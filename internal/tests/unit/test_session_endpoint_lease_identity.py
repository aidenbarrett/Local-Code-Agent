from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_lease import (
    EndpointArbiter,
    EndpointLeaseConflict,
    EndpointRequest,
    EndpointRole,
)


def _active_lease():
    arbiter = EndpointArbiter("ovms://ptl-npu")
    request = EndpointRequest(
        request_id=str(uuid4()),
        endpoint_id="ovms://ptl-npu",
        role=EndpointRole.CONVERSATION,
        session_id="conversation-1",
    )
    arbiter.enqueue(request)
    lease = arbiter.acquire_next()
    assert lease is not None
    return arbiter, lease


def test_active_endpoint_cannot_be_quarantined_without_exact_lease_identity():
    arbiter, lease = _active_lease()
    with pytest.raises(EndpointLeaseConflict, match="exact lease identity"):
        arbiter.quarantine("inference_timeout")
    arbiter.quarantine("inference_timeout", lease_id=lease.lease_id)


def test_active_quarantine_cannot_be_reconciled_without_exact_lease_identity():
    arbiter, lease = _active_lease()
    arbiter.quarantine("inference_timeout", lease_id=lease.lease_id)
    with pytest.raises(EndpointLeaseConflict, match="exact lease identity"):
        arbiter.reconcile_quarantine(known_stopped=True)
    assert arbiter.reconcile_quarantine(
        known_stopped=True,
        lease_id=lease.lease_id,
    )


def test_idle_endpoint_quarantine_has_no_fake_lease_to_supply():
    arbiter = EndpointArbiter("ovms://ptl-npu")
    arbiter.quarantine("runtime_restart_required")
    assert arbiter.reconcile_quarantine(known_stopped=True)
