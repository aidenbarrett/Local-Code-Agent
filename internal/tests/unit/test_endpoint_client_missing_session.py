from __future__ import annotations

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_worker_factory_refuses_missing_session_identity():
    with pytest.raises(ValueError, match="requires a session id"):
        ManagedWorkerClientFactory(
            lambda: object(),
            EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            session_id="",
        )
