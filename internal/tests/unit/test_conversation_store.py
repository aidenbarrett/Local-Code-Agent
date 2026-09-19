from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from local_agent.llm.models import ChatResponse
from local_agent.session.contracts import TaskResult
from local_agent.session.conversation_store import (
    append_turn,
    canonical_turn_bytes,
    conversation,
    create_session,
    new_session,
    turn_ref,
)
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.conversation_gateway import ConversationGateway


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        return ChatResponse(content=next(self.responses))


def _reply(kind: str, text: str) -> str:
    return json.dumps({"kind": kind, "text": text})


def test_turn_ref_is_hash_of_canonical_stored_turn_and_survives_reload(tmp_path):
    session = new_session("profile", "model", "NPU")
    append_turn(session, "user", "héllo 😀", 0)
    expected_bytes = canonical_turn_bytes(session.turns[0])
    ref_before = turn_ref(session, 0)
    assert ref_before == {
        "conversation_id": session.conversation_id,
        "turn_index": 0,
        "turn_sha256": hashlib.sha256(expected_bytes).hexdigest(),
    }
    create_session(tmp_path, session)
    with conversation(tmp_path, session.conversation_id) as opened:
        assert turn_ref(opened.session, 0) == ref_before


def test_prompt_trimming_does_not_change_stored_turn_ref(tmp_path):
    session = new_session("profile", "model", "NPU", budget_chars=64)
    for role, text in [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]:
        append_turn(session, role, text, 0)
    ref = turn_ref(session, 2)
    create_session(tmp_path, session)
    with conversation(tmp_path, session.conversation_id) as opened:
        gateway = ConversationGateway(
            Chat(_reply("reply", "ok")),
            None,
            EventBuffer("s"),
            history_chars=25,
            request_chars=10_000,
            conversation=opened,
        )
        gateway.turn("third")
        assert turn_ref(opened.session, 2) == ref


def test_repository_result_is_not_persisted_as_model_authored_assistant_turn(tmp_path):
    session = new_session("profile", "model", "NPU")
    create_session(tmp_path, session)

    class Controller:
        def run(self, task, **kwargs):
            return TaskResult("task", "fail", "Compiler rejected secret.cpp", False)

    with conversation(tmp_path, session.conversation_id) as opened:
        gateway = ConversationGateway(
            Chat(_reply("repository", "Inspect secret.cpp")),
            Controller(),
            EventBuffer("s"),
            conversation=opened,
        )
        shown = gateway.turn("Why does secret.cpp fail?")
        assert "Compiler rejected" in shown
        assert gateway.last_turn_ref == turn_ref(opened.session, 0)
        assert [(turn.role, turn.content) for turn in opened.session.turns] == [
            ("user", "Why does secret.cpp fail?")
        ]

    with conversation(tmp_path, session.conversation_id) as reopened:
        assert [(turn.role, turn.content) for turn in reopened.session.turns] == [
            ("user", "Why does secret.cpp fail?")
        ]
        assert all("Compiler rejected" not in turn.content for turn in reopened.session.turns)


def test_followup_can_use_bounded_task_observation_without_storing_it_as_raw_history():
    class Controller:
        def run(self, task, **kwargs):
            return TaskResult("task", "fail", "Compiler rejected foo.cpp", False)

    chat = Chat(
        _reply("repository", "Inspect foo.cpp"),
        _reply("reply", "That explains the failure."),
    )
    gateway = ConversationGateway(chat, Controller(), EventBuffer("s"))
    gateway.turn("Inspect foo.cpp")
    gateway.turn("Why?")
    second_prompt = chat.calls[1]
    observation = next(
        item for item in second_prompt
        if item["role"] == "system" and item["content"].startswith("Historical task observation")
    )
    assert "Compiler rejected foo.cpp" in observation["content"]
    assert "[Controller:" not in json.dumps(second_prompt)
    assert all("Compiler rejected foo.cpp" not in turn.content for turn in gateway.session.turns)