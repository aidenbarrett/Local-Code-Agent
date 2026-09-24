from __future__ import annotations

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_worker_client_refuses_non_uuid_task_identity():
    with pytest.raises(ValueError):
        ManagedLLMClient(
            object(), EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            role=EndpointRole.WORKER, task_id="not-a-uuid", execution_epoch=0,
        )
