from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.conversation_store import (
    ContextRefusal,
    conversation,
    create_session,
    new_session,
    turn_ref,
)
from local_agent.session.durable_history import (
    DurableConversationGateway,
    DurableTaskHistory,
    ResultSummaryRecordingController,
    TurnTaskSessionStore,
)
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.event_contract import build_event
from local_agent.session.session_event_service import DurableSessionService, DurableTaskExecutor
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return next(self.responses)


def _reply(kind: str, text: str) -> ChatResponse:
    return ChatResponse(content=json.dumps({"kind": kind, "text": text}))


def _artifact(data: bytes = b"request") -> dict:
    return {
        "artifact_id": str(uuid4()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "availability": "unavailable",
    }


def _admitted_event(
    *,
    stream_id: str,
    session_id: str,
    task_id: str,
    ref: dict[str, object],
    sequence: int = 1,
) -> dict:
    return build_event(
        stream_id=stream_id,
        sequence=sequence,
        producer_epoch=str(uuid4()),
        session_id=session_id,
        task_id=task_id,
        kind="task.admitted",
        payload={
            "origin": {"kind": "user_direct", "turn_ref": ref},
            "request_ref": _artifact(),
            "contract_sha256": "b" * 64,
            "repository_id": "repo-1",
            "skill": None,
            "execution_epoch": 0,
            "deadline_utc": "2030-01-01T00:00:00Z",
        },
    )


def test_turn_task_index_is_part_of_the_admission_commit(tmp_path):
    stream_id = str(uuid4())
    session_id = str(uuid4())
    task_id = str(uuid4())
    ref = {
        "conversation_id": "conversation-1",
        "turn_index": 3,
        "turn_sha256": "a" * 64,
    }
    store = TurnTaskSessionStore(tmp_path / "session.db")
    event = _admitted_event(
        stream_id=stream_id,
        session_id=session_id,
        task_id=task_id,
        ref=ref,
    )

    got, created = store.admit(
        request_id="request-1",
        payload_sha256="c" * 64,
        envelope=event,
        expected_sequence=1,
    )

    assert (got, created) == (task_id, True)
    assert store.task_ids_for_turn(stream_id, ref) == [task_id]
    assert store.replay(stream_id) == [event]


def test_upgrade_rebuilds_turn_task_index_from_existing_admission_events(tmp_path):
    stream_id = str(uuid4())
    session_id = str(uuid4())
    task_id = str(uuid4())
    ref = {
        "conversation_id": "conversation-before-upgrade",
        "turn_index": 0,
        "turn_sha256": "d" * 64,
    }
    path = tmp_path / "session.db"
    old_store = SQLiteSessionStore(path)
    old_store.admit(
        request_id="request-before-upgrade",
        payload_sha256="e" * 64,
        envelope=_admitted_event(
            stream_id=stream_id,
            session_id=session_id,
            task_id=task_id,
            ref=ref,
        ),
        expected_sequence=1,
    )

    upgraded = TurnTaskSessionStore(path)

    assert upgraded.task_ids_for_turn(stream_id, ref) == [task_id]


def test_restart_reconstructs_followup_from_durable_state_not_last_result(tmp_path, loaded):
    _sandbox, repo, _reg, _evidence, _skills = loaded
    runtime_root = tmp_path / "runtime"
    database = runtime_root / "session-hub" / "session.db"
    session = new_session("test", "conversation-model", "cpu")
    create_session(runtime_root, session)
    conversation_id = session.conversation_id
    stream_id = str(uuid4())
    session_id = str(uuid4())

    class Controller:
        allow_execution = False
        context_budget_tokens = 12_000

        def __init__(self):
            self.repo = repo

        def run(self, task, *, self_check=False, route_source=None, task_id=None):
            assert task_id is not None
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "Compiler rejected foo.py at line 17.",
                False,
                verification_ran=True,
            )

    service = DurableSessionService(
        TurnTaskSessionStore(database),
        stream_id=stream_id,
        session_id=session_id,
    )
    try:
        with conversation(runtime_root, conversation_id) as opened:
            controller = ResultSummaryRecordingController(Controller(), service.store)
            runner = DurableTaskAdmissionRunner(DurableTaskExecutor(service, controller))
            first_chat = Chat(_reply("repository", "Inspect foo.py"))
            gateway = DurableConversationGateway(
                first_chat,
                controller,
                EventBuffer("first"),
                conversation=opened,
                task_runner=runner,
                task_history=DurableTaskHistory(service.store, stream_id=stream_id),
            )
            answer = gateway.turn("Inspect foo.py")
            assert "Compiler rejected foo.py" in answer
            first_task_id = gateway.last_result.task_id
    finally:
        service.close()

    # New store, service, conversation object and gateway: no process-local result
    # survives this boundary. The next prompt must reconstruct history from SQLite.
    restarted_service = DurableSessionService(
        TurnTaskSessionStore(database),
        stream_id=stream_id,
        session_id=session_id,
    )
    try:
        with conversation(runtime_root, conversation_id) as reopened:
            second_chat = Chat(_reply("reply", "Because the compiler rejected that line."))
            controller = ResultSummaryRecordingController(Controller(), restarted_service.store)
            gateway = DurableConversationGateway(
                second_chat,
                controller,
                EventBuffer("second"),
                conversation=reopened,
                task_runner=DurableTaskAdmissionRunner(
                    DurableTaskExecutor(restarted_service, controller)
                ),
                task_history=DurableTaskHistory(
                    restarted_service.store,
                    stream_id=stream_id,
                ),
            )
            gateway.last_result = TaskResult(
                "process-local-poison",
                TaskOutcome.FAIL,
                "THIS MUST NEVER ENTER THE FOLLOW-UP PROMPT",
                False,
            )
            result = gateway.turn("Why?")

            assert "Conversation only" in result
            messages = second_chat.calls[0][0]
            prompt = "\n".join(message["content"] for message in messages)
            assert "Compiler rejected foo.py at line 17." in prompt
            assert first_task_id in prompt
            assert "THIS MUST NEVER ENTER THE FOLLOW-UP PROMPT" not in prompt
    finally:
        restarted_service.close()


def test_durable_history_rejects_modified_canonical_turn(tmp_path):
    stream_id = str(uuid4())
    session_id = str(uuid4())
    session = new_session("test", "model", "cpu")
    from local_agent.session.conversation_store import append_turn

    append_turn(session, "user", "original", 0)
    ref = turn_ref(session, 0)
    task_id = str(uuid4())
    store = TurnTaskSessionStore(tmp_path / "session.db")
    store.admit(
        request_id="request-1",
        payload_sha256="f" * 64,
        envelope=_admitted_event(
            stream_id=stream_id,
            session_id=session_id,
            task_id=task_id,
            ref=ref,
        ),
        expected_sequence=1,
    )
    session.turns[0] = replace(session.turns[0], content="modified after admission")

    with pytest.raises(ContextRefusal, match="no longer matches"):
        DurableTaskHistory(store, stream_id=stream_id).render(session)


def test_public_session_uses_turn_indexed_restart_safe_history():
    repo_root = Path(__file__).resolve().parents[3]
    source = (repo_root / "internal" / "scripts" / "session-hub.py").read_text(encoding="utf-8")

    assert "TurnTaskSessionStore(" in source
    assert "ResultSummaryRecordingController(" in source
    assert "DurableTaskHistory(" in source
    assert "DurableConversationGateway(" in source
    assert "task_history=task_history" in source
    assert "durable and restart-safe" in source
