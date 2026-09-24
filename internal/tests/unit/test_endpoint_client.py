from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole, EndpointUnavailable
from local_agent.session.endpoint_runtime import EndpointRuntime


class RecordingClient:
    def __init__(self, *, failure: Exception | None = None):
        self.calls = []
        self.failure = failure

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls.append((messages, tools, max_tokens))
        if self.failure is not None:
            raise self.failure
        return "reply"


def _adapter(endpoint_id="http://127.0.0.1:8000/v1"):
    runtime = EndpointRuntime(EndpointArbiter(endpoint_id))
    return runtime, EndpointCallAdapter(runtime)


def test_conversation_call_uses_and_releases_canonical_endpoint_lease():
    runtime, adapter = _adapter()
    raw = RecordingClient()
    client = ManagedLLMClient(
        raw,
        adapter,
        role=EndpointRole.CONVERSATION,
        session_id=str(uuid4()),
    )

    assert client.chat([{"role": "user", "content": "hello"}], None, 32) == "reply"
    assert raw.calls == [([{"role": "user", "content": "hello"}], None, 32)]
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False


def test_worker_call_carries_task_authority_through_same_endpoint_runtime():
    runtime, adapter = _adapter()
    raw = RecordingClient()
    task_id = str(uuid4())
    client = ManagedLLMClient(
        raw,
        adapter,
        role=EndpointRole.WORKER,
        session_id=str(uuid4()),
        task_id=task_id,
        execution_epoch=3,
    )

    assert client.chat([], [{"type": "function"}]) == "reply"
    assert runtime.arbiter.active_lease is None


def test_transport_exception_quarantines_endpoint_instead_of_freeing_it():
    runtime, adapter = _adapter()
    client = ManagedLLMClient(
        RecordingClient(failure=RuntimeError("transport gone")),
        adapter,
        role=EndpointRole.CONVERSATION,
        session_id=str(uuid4()),
    )

    with pytest.raises(RuntimeError, match="transport gone"):
        client.chat([])

    assert runtime.arbiter.quarantined is True
    assert runtime.arbiter.active_lease is not None
    with pytest.raises(EndpointUnavailable):
        ManagedLLMClient(
            RecordingClient(),
            adapter,
            role=EndpointRole.CONVERSATION,
            session_id=str(uuid4()),
        ).chat([])


def test_conversation_client_refuses_task_execution_authority():
    _runtime, adapter = _adapter()
    with pytest.raises(ValueError, match="cannot carry task authority"):
        ManagedLLMClient(
            RecordingClient(),
            adapter,
            role=EndpointRole.CONVERSATION,
            session_id=str(uuid4()),
            task_id=str(uuid4()),
            execution_epoch=0,
        )


def test_worker_client_refuses_missing_task_identity():
    _runtime, adapter = _adapter()
    with pytest.raises(ValueError, match="requires a task id"):
        ManagedLLMClient(RecordingClient(), adapter, role=EndpointRole.WORKER)
