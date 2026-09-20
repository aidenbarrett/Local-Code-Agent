from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService, DurableTaskExecutor
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import (
    DurableTaskAdmissionRunner,
    execution_contract_sha256,
    repository_id,
)


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def chat(self, messages, tools=None):
        return next(self.responses)


def _reply(kind: str, text: str) -> ChatResponse:
    return ChatResponse(content=json.dumps({"kind": kind, "text": text}))


def _turn_ref(index: int = 0) -> dict[str, object]:
    return {
        "conversation_id": "conversation-1",
        "turn_index": index,
        "turn_sha256": f"{index + 1:064x}",
    }


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_gateway_passes_saved_turn_ref_to_explicit_task_runner():
    calls = []

    class Runner:
        def run(self, task, **kwargs):
            calls.append((task, kwargs))
            return TaskResult("task-1", TaskOutcome.FAIL, "observed failure", False)

    controller = SimpleNamespace(
        run=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct controller path bypassed task runner")
        )
    )
    gateway = ConversationGateway(
        Chat(_reply("repository", "Inspect repository")),
        controller,
        EventBuffer("s"),
        task_runner=Runner(),
    )
    gateway.turn("inspect repository")
    assert len(calls) == 1
    task, kwargs = calls[0]
    assert "inspect repository" in task
    assert kwargs["route_source"] == RouteSource.MODEL_PROPOSAL
    assert kwargs["self_check"] is False
    assert kwargs["turn_ref"] == gateway.last_turn_ref
    assert kwargs["turn_ref"]["turn_index"] == 0


def test_direct_check_passes_saved_turn_ref_without_conversation_model():
    calls = []

    class Runner:
        def run(self, task, **kwargs):
            calls.append((task, kwargs))
            return TaskResult("task-1", TaskOutcome.BLOCKED, "blocked", False)

    gateway = ConversationGateway(
        Chat(),
        SimpleNamespace(run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())),
        EventBuffer("s"),
        task_runner=Runner(),
    )
    gateway.turn("/check")
    assert len(calls) == 1
    assert calls[0][1]["route_source"] == RouteSource.USER_DIRECT
    assert calls[0][1]["self_check"] is True
    assert calls[0][1]["turn_ref"] == gateway.last_turn_ref


def test_durable_gateway_runner_commits_admission_before_controller_effects(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    calls = []

    class Controller:
        def __init__(self):
            self.repo = repo
            self.allow_execution = False
            self.context_budget_tokens = 12_000

        def run(self, task, *, self_check=False, route_source=None, task_id=None):
            assert task_id is not None
            record = service.store.task_record(task_id)
            assert record is not None
            assert record["state"] == "running"
            assert record["terminal"] is False
            calls.append((task_id, task, self_check, route_source))
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "verification failed",
                False,
                verification_ran=True,
            )

    try:
        controller = Controller()
        runner = DurableTaskAdmissionRunner(DurableTaskExecutor(service, controller))
        result = runner.run(
            "inspect repository",
            turn_ref=_turn_ref(),
            route_source=RouteSource.USER_DIRECT,
        )
        assert result.task_id == calls[0][0]
        events = service.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted",
            "task.state_changed",
            "task.verdict",
            "task.closed",
        ]
        admitted = events[0]["payload"]
        assert admitted["origin"] == {
            "kind": "user_direct",
            "turn_ref": _turn_ref(),
        }
        assert admitted["repository_id"] == repository_id(repo)
        assert admitted["skill"] is None
        assert admitted["request_ref"]["availability"] == "unavailable"
        assert len(admitted["request_ref"]["sha256"]) == 64
        assert len(admitted["contract_sha256"]) == 64
        assert UUID(admitted["request_ref"]["artifact_id"])
    finally:
        service.close()


def test_identical_saved_turn_never_reexecutes_durable_effects(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    calls = []

    class Controller:
        allow_execution = False
        context_budget_tokens = 12_000

        def __init__(self):
            self.repo = repo

        def run(self, task, *, self_check=False, route_source=None, task_id=None):
            calls.append(task_id)
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "failed",
                False,
                verification_ran=True,
            )

    try:
        runner = DurableTaskAdmissionRunner(DurableTaskExecutor(service, Controller()))
        runner.run(
            "inspect repository",
            turn_ref=_turn_ref(),
            route_source=RouteSource.MODEL_PROPOSAL,
        )
        with pytest.raises(RuntimeError, match="already admitted"):
            runner.run(
                "inspect repository",
                turn_ref=_turn_ref(),
                route_source=RouteSource.MODEL_PROPOSAL,
            )
        assert len(calls) == 1
        admitted = [event for event in service.replay() if event["kind"] == "task.admitted"]
        assert len(admitted) == 1
        origin = admitted[0]["payload"]["origin"]
        assert origin["kind"] == "model_proposal"
        UUID(origin["proposal_id"])
    finally:
        service.close()


def test_rule_route_fails_closed_until_rule_identity_exists(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    controller = SimpleNamespace(
        repo=repo,
        allow_execution=False,
        context_budget_tokens=12_000,
        run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    try:
        runner = DurableTaskAdmissionRunner(DurableTaskExecutor(service, controller))
        with pytest.raises(ValueError, match="rule identity"):
            runner.run(
                "build",
                turn_ref=_turn_ref(),
                route_source=RouteSource.RULE,
            )
        assert service.replay() == []
    finally:
        service.close()


def test_execution_contract_tracks_effective_controller_configuration(loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    one = SimpleNamespace(repo=repo, allow_execution=False, context_budget_tokens=12_000)
    two = SimpleNamespace(repo=repo, allow_execution=False, context_budget_tokens=8_000)
    assert execution_contract_sha256(one) != execution_contract_sha256(two)


def test_public_session_composes_durable_task_admission_runner():
    repo_root = Path(__file__).resolve().parents[3]
    source = (repo_root / "internal" / "scripts" / "session-hub.py").read_text(encoding="utf-8")
    assert "DurableTaskExecutor(service, controller)" in source
    assert "DurableTaskAdmissionRunner(" in source
    assert "task_runner=task_runner" in source
    assert "allow_execution=args.allow_execution" in source
    assert "context_budget_tokens=worker_config.context_budget_tokens" in source
    assert "durable admission enabled before controller effects" in source
    assert "synchronous prototype path" not in source
