from __future__ import annotations

import json
from types import SimpleNamespace

from local_agent.llm.protocol import ChatResponse
from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import append_turn, turn_ref
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_BUILD_AND_TEST, RULE_TASK_DIAGNOSTIC
from local_agent.session.task_history import TaskCandidate, TaskObservation


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return next(self.responses)


def _reply(kind: str, text: str) -> ChatResponse:
    return ChatResponse(content=json.dumps({"kind": kind, "text": text}))


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return TaskResult("task-result", TaskOutcome.FAIL, "failed", False)


def _gateway(chat=None, runner=None, task_history=None):
    return ConversationGateway(
        chat or Chat(),
        SimpleNamespace(repo=object(), run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())),
        EventBuffer("s"),
        task_runner=runner,
        task_history=task_history,
    )


def test_exact_build_rule_bypasses_conversation_model():
    chat = Chat()
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    gateway.turn("Build it")
    assert chat.calls == 0
    assert len(runner.calls) == 1
    task, kwargs = runner.calls[0]
    assert "rule_id=build-and-test/v1" in task
    assert kwargs["route_source"] == RouteSource.RULE
    assert kwargs["rule_id"] == RULE_BUILD_AND_TEST
    assert kwargs["skill"] == "build-and-test"


def test_unmatched_request_still_uses_model_fallback():
    chat = Chat(_reply("repository", "Inspect the cache implementation"))
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    gateway.turn("Could you look into the weird cache issue?")
    assert chat.calls == 1
    assert len(runner.calls) == 1
    assert runner.calls[0][1]["route_source"] == RouteSource.MODEL_PROPOSAL
    assert "Conversation proposal (untrusted)" in runner.calls[0][0]


def test_explicit_chat_never_starts_work_even_if_model_proposes_repository():
    chat = Chat(_reply("repository", "I would inspect the repository"))
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    answer = gateway.turn("Build it", explicit_mode="chat")
    assert chat.calls == 1
    assert runner.calls == []
    assert "Conversation only" in answer


def test_explicit_work_bypasses_model_and_preserves_user_text():
    chat = Chat()
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    text = "  Inspect src/Foo.cpp exactly as written  "
    gateway.turn(text, explicit_mode="work")
    assert chat.calls == 0
    assert len(runner.calls) == 1
    task, kwargs = runner.calls[0]
    assert task == "User request:\n" + text
    assert kwargs["route_source"] == RouteSource.USER_DIRECT
    assert gateway.session.turns[0].content == text


class History:
    def __init__(self, candidates, observations):
        self._candidates = tuple(candidates)
        self._observations = observations

    def latest(self, conversation_id):
        return None

    def failure_candidates(self, conversation_id):
        return self._candidates

    def observation_for(self, candidate):
        return self._observations.get(candidate.task_id)


def _prior_turn(gateway, text: str):
    index = len(gateway.session.turns)
    append_turn(gateway.session, "user", text, gateway.runtime_index)
    return turn_ref(gateway.session, index)


def _observation(task_id: str, ref: dict[str, object]) -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        turn_ref=ref,
        status="failed",
        verdict="FAILED",
        answer=f"historical result for {task_id}",
        evidence_ids=(f"evidence:{task_id}",),
    )


def test_failed_task_diagnostic_routes_only_with_one_verified_candidate():
    chat = Chat()
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    ref = _prior_turn(gateway, "first failed task")
    candidate = TaskCandidate("task-1", ref)
    gateway.task_history = History((candidate,), {"task-1": _observation("task-1", ref)})

    gateway.turn("Why did that fail?")
    assert chat.calls == 0
    assert len(runner.calls) == 1
    task, kwargs = runner.calls[0]
    assert kwargs["route_source"] == RouteSource.RULE
    assert kwargs["rule_id"] == RULE_TASK_DIAGNOSTIC
    assert kwargs["skill"] == "task-diagnostic"
    assert "Task task-1" in task
    assert "untrusted historical data" in task


def test_multiple_failed_task_candidates_clarify_without_model_or_worker():
    chat = Chat()
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    ref1 = _prior_turn(gateway, "first failed task")
    ref2 = _prior_turn(gateway, "second failed task")
    one = TaskCandidate("task-1", ref1)
    two = TaskCandidate("task-2", ref2)
    gateway.task_history = History(
        (one, two),
        {
            "task-1": _observation("task-1", ref1),
            "task-2": _observation("task-2", ref2),
        },
    )

    answer = gateway.turn("Why did that fail?")
    assert "multiple durable failed tasks" in answer
    assert chat.calls == 0
    assert runner.calls == []


def test_stale_failed_task_candidate_is_not_routing_authority():
    chat = Chat()
    runner = Runner()
    gateway = _gateway(chat=chat, runner=runner)
    ref = _prior_turn(gateway, "failed task")
    stale = dict(ref)
    stale["turn_sha256"] = "f" * 64
    candidate = TaskCandidate("task-1", stale)
    gateway.task_history = History((candidate,), {"task-1": _observation("task-1", stale)})

    answer = gateway.turn("Why did that fail?")
    assert "No eligible durable failed task" in answer
    assert chat.calls == 0
    assert runner.calls == []
