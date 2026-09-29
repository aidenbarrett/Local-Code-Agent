from __future__ import annotations

from dataclasses import replace

from local_agent.config import MODEL_PRESETS
from local_agent.llm.protocol import LLMTransportError
from uuid import uuid4

import pytest

from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.runtime_facts import RuntimeFacts
from local_agent.session.candidate_facts import CandidateFacts
from local_agent.session.runtime_facts_gateway import RepositoryFacts, RuntimeFactsGateway
from local_agent.session.task_history import RetainedTaskResult


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


@pytest.mark.parametrize("hub", ["base", "runtime_facts"])
def test_turns_after_a_quarantine_are_answered_and_say_how_to_recover(hub):
    runtime = EndpointRuntime(EndpointArbiter("http://127.0.0.1:18010/v3"))
    runtime.arbiter.quarantine("task execution stopped without proof underlying inference stopped")
    model = _NoModelClient()
    client = ManagedLLMClient(model, EndpointCallAdapter(runtime),
                              role=EndpointRole.CONVERSATION, session_id=str(uuid4()))
    events = EventBuffer("quarantined")
    if hub == "base":
        gateway = ConversationGateway(client, _Controller(), events)
    else:
        gateway = RuntimeFactsGateway(client, _Controller(), events, runtime_facts=_facts())

    # Every later turn is answered; none raises out of the gateway.
    for said in ("did the stop work?", "hello there"):
        answer = gateway.turn(said)
        assert "without proof underlying inference stopped" in answer
        assert "Restart the Hub" in answer and "No task was run." in answer
        assert "rephrase" not in answer.lower()
    assert model.calls == 0
    refused = [e for e in events.after(0) if e.kind == "turn.refused"]
    assert [e.payload["reason"] for e in refused] == ["endpoint_quarantined"] * 2


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


@pytest.mark.parametrize("question", [
    "where are you working?",
    "what repository are you working on?",
    "what repo are you working on?",
    "what can you access?",
])
def test_repository_authority_questions_are_deterministic_without_model_calls(tmp_path, question):
    client = _NoModelClient()
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("repository-facts"),
        runtime_facts=_facts(),
        repository_facts=RepositoryFacts(
            name="Local-Code-Agent",
            root=root,
            branch="feature/trust",
            execution_enabled=False,
        ),
    )

    answer = gateway.turn(question)

    assert client.calls == 0
    assert "Active repository: Local-Code-Agent" in answer
    assert f"Root: {root}" in answer
    assert "Observed branch: feature/trust" in answer
    assert "restricted to this root" in answer
    assert "execution is disabled" in answer


def test_repository_question_fails_closed_when_authority_facts_are_missing():
    client = _NoModelClient()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("repository-facts-missing"),
        runtime_facts=_facts(),
    )

    answer = gateway.turn("where are you working?")

    assert client.calls == 0
    assert answer == (
        "I cannot establish the active repository authority for this session. No task was run."
    )


class _LatestResultHistory:
    def __init__(self, result):
        self.result = result

    def latest_result(self, conversation_id):
        assert conversation_id
        return self.result


def _candidate_result(role, *, verified=True):
    return RetainedTaskResult(
        task_id="result-task",
        status="SUCCEEDED",
        verdict="PASSED",
        answer="worker prose must not decide the location",
        evidence_ids=(),
        verification_ran=verified,
        verified_at_completion=verified,
        candidate=CandidateFacts(
            role=role,
            candidate_task_id="11111111-1111-1111-1111-111111111111",
            retained=role == "prepared",
            paths=("src/example.cpp",),
            patch_sha256="a" * 64 if role == "prepared" else None,
            base_commit="b" * 40,
            commit="c" * 40 if role == "committed" else None,
        ),
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("prepared", "has not been applied"),
        ("applied", "was applied"),
        ("committed", "was committed"),
        ("undone", "was undone"),
        ("discarded", "was discarded"),
        ("apply_refused", "apply was refused"),
    ],
)
def test_effect_location_uses_durable_candidate_facts_without_model_calls(
    tmp_path, role, expected
):
    client = _NoModelClient()
    root = (tmp_path / "repo").resolve()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("effect-location"),
        runtime_facts=_facts(),
        repository_facts=RepositoryFacts(
            name="Local-Code-Agent",
            root=root,
            branch="feature/trust",
            execution_enabled=False,
        ),
    )
    gateway.task_history = _LatestResultHistory(_candidate_result(role))
    answer = gateway.turn("where did you save it?")
    assert client.calls == 0
    assert expected in answer
    assert "src/example.cpp" in answer
    assert f"Active repository root: {root}" in answer
    assert "worker prose" not in answer


def test_effect_location_fails_closed_without_durable_candidate():
    client = _NoModelClient()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("effect-location-missing"),
        runtime_facts=_facts(),
    )
    gateway.task_history = _LatestResultHistory(None)
    answer = gateway.turn("where did you put it?")
    assert client.calls == 0
    assert answer == (
        "I have no durable task result proving where a change was saved. No task was run."
    )


def test_unverified_prepared_candidate_is_not_reported_as_saved(tmp_path):
    client = _NoModelClient()
    root = (tmp_path / "repo").resolve()
    gateway = RuntimeFactsGateway(
        client,
        _Controller(),
        EventBuffer("effect-location-unverified"),
        runtime_facts=_facts(),
        repository_facts=RepositoryFacts(
            name="Local-Code-Agent",
            root=root,
            branch="feature/trust",
            execution_enabled=False,
        ),
    )
    gateway.task_history = _LatestResultHistory(
        _candidate_result("prepared", verified=False)
    )
    answer = gateway.turn("where did you save it")
    assert client.calls == 0
    assert "not verified at completion" in answer
    assert "has not been applied" in answer
