from __future__ import annotations

from uuid import uuid4

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


class Worker:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_clean_worker_call_leaves_shared_endpoint_available():
    runtime = EndpointRuntime(EndpointArbiter("endpoint"))
    factory = ManagedWorkerClientFactory(Worker, EndpointCallAdapter(runtime), session_id=str(uuid4()))
    with factory.bind_task(str(uuid4()), 0):
        assert factory().chat([]) == "ok"
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False
