from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_worker_factory_refuses_non_callable_client_factory():
    with pytest.raises(TypeError, match="callable raw factory"):
        ManagedWorkerClientFactory(
            object(), EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            session_id=str(uuid4()),
        )
