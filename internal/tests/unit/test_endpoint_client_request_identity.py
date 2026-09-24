from __future__ import annotations

from uuid import uuid4

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime


class Client:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_each_logical_call_gets_a_new_endpoint_request_identity():
    client = ManagedLLMClient(
        Client(),
        EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint"))),
        role=EndpointRole.CONVERSATION,
        session_id=str(uuid4()),
    )
    first = client._request()
    second = client._request()
    assert first.request_id != second.request_id
