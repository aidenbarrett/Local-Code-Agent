from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_worker_client_refuses_missing_execution_epoch():
    with pytest.raises(ValueError, match="execution epoch"):
        ManagedLLMClient(
            object(), EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            role=EndpointRole.WORKER, task_id=str(uuid4()),
        )
