from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import append_turn, new_session, turn_ref
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.event_contract import build_event
from local_agent.session.session_event_service import DurableSessionService, DurableTaskExecutor
from local_agent.session.session_store import ArtifactIntegrityError, SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_history import DurableTaskHistory, TaskObservation


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return next(self.responses)


def _reply(text: str = "follow-up") -> ChatResponse:
    return ChatResponse(content=json.dumps({"kind": "reply", "text": text}))


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _controller(repo, service, answer="Compiler rejected foo.py"):
    class Controller:
        allow_execution = False
        context_budget_tokens = 12_000

        def __init__(self):
            self.repo = repo

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            assert skill_name is None
            assert task_id is not None
            # Execution can begin only after admission + association are durable.
            assert service.store.task_record(task_id)["state"] == "running"
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                answer,
                False,
                ("compiler:0",),
                verification_ran=True,
            )

    return Controller()


def test_admission_atomically_retains_request_and_exact_turn_association(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    ref = {
        "conversation_id": "conv-1",
        "turn_index": 4,
        "turn_sha256": "a" * 64,
    }
    try:
        runner = DurableTaskAdmissionRunner(
            DurableTaskExecutor(service, _controller(repo, service))
        )
        result = runner.run("inspect foo.py", turn_ref=ref, route_source=RouteSource.USER_DIRECT)
        admitted = service.replay()[0]["payload"]
        request_ref = admitted["request_ref"]

        assert request_ref["availability"] == "retained"
        request_bytes = service.store.artifact_bytes(request_ref)
        assert hashlib.sha256(request_bytes).hexdigest() == request_ref["sha256"]
        request = json.loads(request_bytes)
        assert request["task"] == "inspect foo.py"
        assert request["turn_ref"] == ref
        assert service.store.task_ids_for_turn(ref) == [result.task_id]
        assert service.store.task_ids_for_turn({**ref, "turn_sha256": "b" * 64}) == []
    finally:
        service.close()


def test_retained_request_digest_mismatch_rolls_back_task_association_and_event(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.db")
    stream_id, epoch, session_id, task_id = (str(uuid4()) for _ in range(4))
    request = b"actual request"
    ref = {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(request).hexdigest(),
        "media_type": "application/vnd.lca.task-request+json",
        "size_bytes": len(request),
        "availability": "retained",
    }
    turn = {"conversation_id": "conv-1", "turn_index": 0, "turn_sha256": "a" * 64}
    event = build_event(
        stream_id=stream_id,
        sequence=1,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {"kind": "user_direct", "turn_ref": turn},
            "request_ref": ref,
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": None,
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )

    with pytest.raises(ArtifactIntegrityError, match="payload digest"):
        store.admit(
            request_id="request-1",
            payload_sha256=hashlib.sha256(b"different").hexdigest(),
            envelope=event,
            expected_sequence=1,
            request_bytes=request,
        )

    assert store.task_record(task_id) is None
    assert store.task_ids_for_turn(turn) == []
    assert store.replay(stream_id) == []
    assert store.next_sequence(stream_id) == 1


def test_terminal_result_is_retained_and_reconstructed_after_restart(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    ref = {
        "conversation_id": "conv-1",
        "turn_index": 0,
        "turn_sha256": "a" * 64,
    }
    try:
        runner = DurableTaskAdmissionRunner(
            DurableTaskExecutor(service, _controller(repo, service, "persistent failure detail"))
        )
        result = runner.run("inspect", turn_ref=ref, route_source=RouteSource.USER_DIRECT)
        result_ref = service.store.task_record(result.task_id)["result_ref"]
        assert result_ref["availability"] == "retained"
        assert json.loads(service.store.artifact_bytes(result_ref))["answer"] == "persistent failure detail"
    finally:
        service.close()

    reopened = SQLiteSessionStore(tmp_path / "session.db")
    observation = DurableTaskHistory(reopened).latest("conv-1")
    assert observation is not None
    assert observation.task_id == result.task_id
    assert observation.answer == "persistent failure detail"
    assert observation.verdict == "FAILED"
    assert observation.evidence_ids == ("compiler:0",)


def test_existing_admission_event_backfills_turn_association_without_inventing_bytes(tmp_path):
    store = SQLiteSessionStore(tmp_path / "session.db")
    stream_id, epoch, session_id, task_id = (str(uuid4()) for _ in range(4))
    turn = {"conversation_id": "conv-old", "turn_index": 2, "turn_sha256": "c" * 64}
    request_ref = {
        "artifact_id": str(uuid4()),
        "sha256": "d" * 64,
        "media_type": "application/vnd.lca.task-request+json",
        "size_bytes": 10,
        "availability": "unavailable",
    }
    event = build_event(
        stream_id=stream_id,
        sequence=1,
        producer_epoch=epoch,
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {"kind": "user_direct", "turn_ref": turn},
            "request_ref": request_ref,
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": None,
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )
    store.admit(
        request_id="old-request",
        payload_sha256="d" * 64,
        envelope=event,
        expected_sequence=1,
    )
    with store._connect() as conn:
        conn.execute("DELETE FROM turn_tasks WHERE task_id = ?", (task_id,))
    assert store.task_ids_for_turn(turn) == []

    reopened = SQLiteSessionStore(tmp_path / "session.db")
    assert reopened.task_ids_for_turn(turn) == [task_id]
    with pytest.raises(ArtifactIntegrityError, match="not retained"):
        reopened.artifact_bytes(request_ref)


def test_gateway_routes_one_durable_failure_without_model_after_resume(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    session = new_session("p", "m", "d")
    append_turn(session, "user", "inspect foo.py", 0)
    origin = turn_ref(session, 0)
    service = _service(tmp_path)
    calls = []

    class Runner:
        def run(self, task, **kwargs):
            calls.append((task, kwargs))
            return TaskResult("diagnostic-task", TaskOutcome.BLOCKED, "diagnostic blocked", False)

    try:
        runner = DurableTaskAdmissionRunner(
            DurableTaskExecutor(service, _controller(repo, service, "durable compiler failure"))
        )
        runner.run("inspect foo.py", turn_ref=origin, route_source=RouteSource.USER_DIRECT)

        chat = Chat()
        gateway = ConversationGateway(
            chat,
            SimpleNamespace(repo=repo, run=lambda *a, **k: (_ for _ in ()).throw(AssertionError())),
            EventBuffer("s"),
            task_runner=Runner(),
            task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        )
        gateway.session = session
        gateway.last_result = None
        gateway.turn("Why did that fail?")

        assert chat.calls == []
        assert len(calls) == 1
        task, kwargs = calls[0]
        assert kwargs["route_source"] == RouteSource.RULE
        assert kwargs["rule_id"] == "task-diagnostic/v1"
        assert "durable compiler failure" in task
        assert "untrusted historical data" in task
        assert [turn.role for turn in session.turns] == ["user", "user"]
    finally:
        service.close()


def test_durable_failure_candidates_do_not_hide_ambiguity(tmp_path, loaded):
    _sandbox, repo, _reg, _store, _skills = loaded
    service = _service(tmp_path)
    try:
        runner = DurableTaskAdmissionRunner(
            DurableTaskExecutor(service, _controller(repo, service))
        )
        one = {
            "conversation_id": "conv-1",
            "turn_index": 0,
            "turn_sha256": "a" * 64,
        }
        two = {
            "conversation_id": "conv-1",
            "turn_index": 1,
            "turn_sha256": "b" * 64,
        }
        first = runner.run("first", turn_ref=one, route_source=RouteSource.USER_DIRECT)
        second = runner.run("second", turn_ref=two, route_source=RouteSource.USER_DIRECT)
        history = DurableTaskHistory(service.store, stream_id=service.stream_id)
        candidates = history.failure_candidates("conv-1")
        assert tuple(candidate.task_id for candidate in candidates) == (first.task_id, second.task_id)
        assert candidates[0].turn_ref == one
        assert candidates[1].turn_ref == two
        assert history.observation_for(candidates[0]).answer == "Compiler rejected foo.py"
    finally:
        service.close()


def test_gateway_omits_task_history_when_turn_digest_does_not_match_raw_conversation():
    session = new_session("p", "m", "d")
    append_turn(session, "user", "real turn", 0)

    class History:
        def latest(self, conversation_id):
            return TaskObservation(
                task_id=str(uuid4()),
                turn_ref={
                    "conversation_id": conversation_id,
                    "turn_index": 0,
                    "turn_sha256": "f" * 64,
                },
                status="failed",
                verdict="FAILED",
                answer="must not reach model",
                evidence_ids=(),
            )

    events = EventBuffer("s")
    chat = Chat(_reply())
    gateway = ConversationGateway(chat, None, events, task_history=History())
    gateway.session = session
    gateway.turn("follow up")

    assert all("must not reach model" not in item["content"] for item in chat.calls[0][0])
    assert any(event.kind == "conversation.task_history_unavailable" for event in events.after(0))
