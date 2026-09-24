from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime


class Broken:
    def chat(self, messages, tools=None, max_tokens=None):
        raise RuntimeError("broken")


class Healthy:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_split_endpoint_quarantine_is_scoped_to_physical_endpoint():
    first_runtime = EndpointRuntime(EndpointArbiter("first"))
    second_runtime = EndpointRuntime(EndpointArbiter("second"))
    first = ManagedLLMClient(
        Broken(), EndpointCallAdapter(first_runtime),
        role=EndpointRole.CONVERSATION, session_id=str(uuid4()),
    )
    second = ManagedLLMClient(
        Healthy(), EndpointCallAdapter(second_runtime),
        role=EndpointRole.CONVERSATION, session_id=str(uuid4()),
    )
    with pytest.raises(RuntimeError, match="broken"):
        first.chat([])
    assert second.chat([]) == "ok"
    assert first_runtime.arbiter.quarantined is True
    assert second_runtime.arbiter.quarantined is False
