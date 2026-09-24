from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_managed_client_refuses_unknown_role():
    with pytest.raises(ValueError):
        ManagedLLMClient(
            object(), EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            role="unknown", session_id=str(uuid4()),
        )
