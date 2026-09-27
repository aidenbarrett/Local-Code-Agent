from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.llm.protocol import LLMTransportError
from local_agent.session.endpoint_client import (
    ManagedLLMClient,
    ManagedWorkerClientFactory,
    ModelEndpointQuarantinedError,
)
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
    client = ManagedLLMClient(raw, adapter, role=EndpointRole.CONVERSATION, session_id=str(uuid4()))
    assert client.chat([{"role": "user", "content": "hello"}], None, 32) == "reply"
    assert raw.calls == [([{"role": "user", "content": "hello"}], None, 32)]
    assert runtime.arbiter.active_lease is None
    assert runtime.arbiter.quarantined is False


def test_worker_call_carries_task_authority_through_same_endpoint_runtime():
    runtime, adapter = _adapter()
    raw = RecordingClient()
    client = ManagedLLMClient(
        raw, adapter, role=EndpointRole.WORKER, session_id=str(uuid4()),
        task_id=str(uuid4()), execution_epoch=3,
    )
    assert client.chat([], [{"type": "function"}]) == "reply"
    assert runtime.arbiter.active_lease is None


def test_worker_factory_requires_durable_authority_and_clears_it_after_scope():
    runtime, adapter = _adapter()
    raw = RecordingClient()
    factory = ManagedWorkerClientFactory(lambda: raw, adapter, session_id=str(uuid4()))
    with pytest.raises(RuntimeError, match="admitted task authority"):
        factory()
    task_id = str(uuid4())
    with factory.bind_task(task_id, 4):
        assert factory().chat([]) == "reply"
    assert runtime.arbiter.active_lease is None
    with pytest.raises(RuntimeError, match="admitted task authority"):
        factory()


def test_transport_exception_quarantines_endpoint_instead_of_freeing_it():
    runtime, adapter = _adapter()
    client = ManagedLLMClient(
        RecordingClient(failure=RuntimeError("transport gone")), adapter,
        role=EndpointRole.CONVERSATION, session_id=str(uuid4()),
    )
    with pytest.raises(RuntimeError, match="transport gone"):
        client.chat([])
    assert runtime.arbiter.quarantined is True
    assert runtime.arbiter.active_lease is not None
    after = RecordingClient()
    with pytest.raises(ModelEndpointQuarantinedError, match="endpoint is quarantined") as refused:
        ManagedLLMClient(after, adapter, role=EndpointRole.CONVERSATION, session_id=str(uuid4())).chat([])
    # Nothing was sent, and the refusal is a transport failure every caller handles.
    assert after.calls == []
    assert isinstance(refused.value, LLMTransportError) and refused.value.kind == "unavailable"
    assert isinstance(refused.value.__cause__, EndpointUnavailable)


def test_a_worker_refused_by_a_quarantined_endpoint_sees_an_unavailable_model():
    runtime, adapter = _adapter()
    runtime.arbiter.quarantine("task execution stopped without proof underlying inference stopped")
    raw = RecordingClient()
    client = ManagedLLMClient(
        raw, adapter, role=EndpointRole.WORKER, session_id=str(uuid4()),
        task_id=str(uuid4()), execution_epoch=0,
    )
    with pytest.raises(LLMTransportError, match="without proof underlying inference stopped"):
        client.chat([], [{"type": "function"}])
    assert raw.calls == []


def test_a_model_client_error_is_not_mistaken_for_a_quarantine_refusal():
    _runtime, adapter = _adapter()
    client = ManagedLLMClient(
        RecordingClient(failure=EndpointUnavailable("raised by the model client itself")), adapter,
        role=EndpointRole.CONVERSATION, session_id=str(uuid4()),
    )
    with pytest.raises(EndpointUnavailable) as raised:
        client.chat([])
    assert not isinstance(raised.value, LLMTransportError)


def test_conversation_client_refuses_task_execution_authority():
    _runtime, adapter = _adapter()
    with pytest.raises(ValueError, match="cannot carry task authority"):
        ManagedLLMClient(
            RecordingClient(), adapter, role=EndpointRole.CONVERSATION,
            session_id=str(uuid4()), task_id=str(uuid4()), execution_epoch=0,
        )


def test_worker_client_refuses_missing_task_identity():
    _runtime, adapter = _adapter()
    with pytest.raises(ValueError, match="requires a task id"):
        ManagedLLMClient(RecordingClient(), adapter, role=EndpointRole.WORKER)
