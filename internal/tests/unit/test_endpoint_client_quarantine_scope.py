from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient, ManagedWorkerClientFactory
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole, EndpointUnavailable
from local_agent.session.endpoint_runtime import EndpointRuntime


class BrokenChat:
    def chat(self, messages, tools=None, max_tokens=None):
        raise RuntimeError("uncertain inference")


class Worker:
    def chat(self, messages, tools=None, max_tokens=None):
        return "must not run"


def test_chat_uncertainty_fences_worker_on_same_physical_endpoint():
    runtime = EndpointRuntime(EndpointArbiter("endpoint"))
    adapter = EndpointCallAdapter(runtime)
    chat = ManagedLLMClient(BrokenChat(), adapter, role=EndpointRole.CONVERSATION, session_id=str(uuid4()))
    worker = ManagedWorkerClientFactory(Worker, adapter, session_id=str(uuid4()))

    with pytest.raises(RuntimeError, match="uncertain inference"):
        chat.chat([])
    with worker.bind_task(str(uuid4()), 0):
        with pytest.raises(EndpointUnavailable):
            worker().chat([])
