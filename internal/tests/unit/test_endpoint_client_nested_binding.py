from __future__ import annotations

from uuid import uuid4

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


class Client:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_nested_worker_authority_restores_outer_scope():
    runtime = EndpointRuntime(EndpointArbiter("endpoint"))
    factory = ManagedWorkerClientFactory(Client, EndpointCallAdapter(runtime), session_id=str(uuid4()))
    outer = str(uuid4())
    inner = str(uuid4())
    with factory.bind_task(outer, 1):
        outer_client = factory()
        assert outer_client._task_id == outer
        with factory.bind_task(inner, 2):
            assert factory()._task_id == inner
        assert factory()._task_id == outer
