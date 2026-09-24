from __future__ import annotations

from dataclasses import replace

from local_agent.config import MODEL_PRESETS
from local_agent.llm.protocol import LLMTransportError
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.runtime_facts import RuntimeFacts
from local_agent.session.runtime_facts_gateway import RuntimeFactsGateway


class _NoModelClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("deterministic product facts must not call the model")


class _FailingClient:
    def chat(self, *args, **kwargs):
        raise LLMTransportError("connection refused", cause="ConnectionRefusedError")


class _Controller:
    pass


def _facts(*, observed_model: str | None = "served-qwen") -> RuntimeFacts:
    return RuntimeFacts(
        preset="ptl-npu-8b",
        endpoint="http://127.0.0.1:18010/v3",
        declared_model="declared-qwen",
        declared_device="NPU",
        observed_model=observed_model,
        execution_enabled=False,
    )


def test_help_and_runtime_identity_are_deterministic_without_model_calls():
    client = _NoModelClient()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("runtime-facts"),
        runtime_facts=_facts(),
    )

    help_answer = gateway.turn("help")
    runtime_answer = gateway.turn("what are you running on?")

    assert client.calls == 0
    assert "read-only tools" in help_answer
    assert "Observed served model: served-qwen" in runtime_answer
    assert "Declared device: NPU" in runtime_answer
    assert "not hardware-utilisation proof" in runtime_answer


def test_transport_failure_names_the_endpoint_and_never_says_rephrase():
    gateway = RuntimeFactsGateway(
        _FailingClient(),
        _Controller(),
        EventBuffer("runtime-outage"),
        runtime_facts=_facts(observed_model=None),
    )

    answer = gateway.turn("hello there")

    assert answer == (
        "Model endpoint unreachable at http://127.0.0.1:18010/v3. "
        "Reason: endpoint_unavailable. No task was run."
    )
    assert "rephrase" not in answer.lower()


def test_runtime_header_uses_observed_models_response_and_marks_device_declared():
    config = replace(
        MODEL_PRESETS["ptl-npu-8b"],
        base_url="http://127.0.0.1:9999/v3",
        model="configured-model",
        device="NPU",
    )
    requested_urls: list[str] = []

    def fetch(url: str) -> bytes:
        requested_urls.append(url)
        return b'{"data":[{"id":"actually-served-model"}]}'

    facts = RuntimeFacts.observe(
        "ptl-npu-8b",
        config,
        execution_enabled=True,
        fetch=fetch,
    )

    assert requested_urls == ["http://127.0.0.1:9999/v1/models"]
    assert facts.observed_model == "actually-served-model"
    assert facts.header() == (
        "model actually-served-model (observed) · device NPU (declared) · "
        "endpoint http://127.0.0.1:9999/v3 · execution enabled"
    )
