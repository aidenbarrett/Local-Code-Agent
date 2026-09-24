from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_worker_factory_refuses_boolean_execution_epoch():
    factory = ManagedWorkerClientFactory(
        lambda: object(),
        EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
        session_id=str(uuid4()),
    )
    with pytest.raises(ValueError, match="nonnegative execution epoch"):
        with factory.bind_task(str(uuid4()), True):
            pass
