from __future__ import annotations

from uuid import uuid4

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


class Client:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_nested_binding_restores_outer_scope_behaviorally():
    runtime = EndpointRuntime(EndpointArbiter("endpoint"))
    factory = ManagedWorkerClientFactory(Client, EndpointCallAdapter(runtime), session_id=str(uuid4()))
    with factory.bind_task(str(uuid4()), 1):
        assert factory().chat([]) == "ok"
        with factory.bind_task(str(uuid4()), 2):
            assert factory().chat([]) == "ok"
        assert factory().chat([]) == "ok"
    assert runtime.arbiter.active_lease is None
