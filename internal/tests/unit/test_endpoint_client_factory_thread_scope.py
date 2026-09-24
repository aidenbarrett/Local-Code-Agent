from __future__ import annotations

from threading import Thread
from uuid import uuid4

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter
from local_agent.session.endpoint_runtime import EndpointRuntime


class Client:
    def chat(self, messages, tools=None, max_tokens=None):
        return "ok"


def test_worker_authority_does_not_leak_to_another_thread():
    adapter = EndpointCallAdapter(EndpointRuntime(EndpointArbiter("endpoint")))
    factory = ManagedWorkerClientFactory(Client, adapter, session_id=str(uuid4()))
    observed = []

    def other_thread():
        try:
            factory()
        except RuntimeError:
            observed.append("refused")

    with factory.bind_task(str(uuid4()), 0):
        thread = Thread(target=other_thread)
        thread.start()
        thread.join()
        assert factory().chat([]) == "ok"

    assert observed == ["refused"]
