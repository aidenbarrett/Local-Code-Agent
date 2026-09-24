from __future__ import annotations

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime


def test_conversation_client_refuses_missing_session():
    with pytest.raises(ValueError, match="requires a session id"):
        ManagedLLMClient(
            object(), EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
            role=EndpointRole.CONVERSATION,
        )
