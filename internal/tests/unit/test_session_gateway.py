import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from local_agent.llm.client import ScriptedClient
from local_agent.llm.models import ChatResponse, ToolCall
from local_agent.session.contracts import (
    OUTCOME_PROJECTIONS,
    UNREACHABLE_TERMINAL_STATES,
    ProductOutcome,
    Proposal,
    RouteSource,
    TaskResult,
    TerminalState,
    task_exit_code,
)
from local_agent.session.task_controller import TaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.conversation_gateway import ConversationGateway


class Chat:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return next(self.responses)


def reply(kind, text):
    return ChatResponse(content=json.dumps({"kind": kind, "text": text}))


@pytest.mark.parametrize("raw", [
    '[]', '{"kind":"shell","text":"ls"}',
    '{"kind":"repository","text":"x","allow_patch":true}',
    '{"kind":"reply","text":"x","kind":"repository"}',
    '{"kind":"reply","text":null}', '```json\n{}\n```',
    '{"kind":"reply","text":""}', '{"kind":"reply","text":123}',
])
def test_proposal_fails_closed(raw):
    with pytest.raises((ValueError, TypeError)):
        Proposal.parse(raw)


def test_task_result_rejects_undeclared_or_unproved_success():
    with pytest.raises(ValueError, match="undeclared product outcome"):
        TaskResult("t", "passed", "wrong spelling")
    with pytest.raises(ValueError, match="verified_at_completion"):
        TaskResult("t", ProductOutcome.PASS, "no proof", False)
    with pytest.raises(ValueError, match="verified_at_completion"):
        TaskResult("t", ProductOutcome.FAIL, "failed", True)


def test_product_outcome_projection_is_total_both_ways():
    assert set(OUTCOME_PROJECTIONS) == set(ProductOutcome)
    produced = {projection.terminal_state for projection in OUTCOME_PROJECTIONS.values()}
    declared = set(TerminalState)
    assert produced | set(UNREACHABLE_TERMINAL_STATES) == declared
    assert produced.isdisjoint(UNREACHABLE_TERMINAL_STATES)
    for state in UNREACHABLE_TERMINAL_STATES:
        assert all(p.terminal_state != state for p in OUTCOME_PROJECTIONS.values())


def test_cli_exit_code_mapping_covers_every_product_outcome():
    codes = {outcome: task_exit_code(outcome) for outcome in ProductOutcome}
    assert set(codes) == set(ProductOutcome)
    assert codes[ProductOutcome.PASS] == 0
    assert codes[ProductOutcome.ESCALATED_PASS] == 0
    assert codes[ProductOutcome.FAIL] == 1
    assert codes[ProductOutcome.ESCALATED_FAIL] == 1
    assert codes[ProductOutcome.BLOCKED] == 2
    assert codes[ProductOutcome.NO_VERDICT] == 2


def test_chat_cannot_call_tools_or_smuggle_policy():
    controller = SimpleNamespace(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no task")))
    model = Chat(ChatResponse(content='{"kind":"repository","text":"inspect"}',
                              tool_calls=[ToolCall("1", "apply_patch", {})]))
    gateway = ConversationGateway(model, controller, EventBuffer("s"))
    assert "No task was run" in gateway.turn("hello")
    assert model.calls[0][1] is None


def test_followup_preserves_controller_result_without_model_recertification():
    tasks = []
    def run(task, **kwargs):
        tasks.append(task)
        return TaskResult("t1", "fail", "Compiler rejected foo.py", False)
    model = Chat(reply("repository", "Inspect foo.py"), reply("reply", "That is a syntax error."))
    gateway = ConversationGateway(model, SimpleNamespace(run=run), EventBuffer("s"))
    answer = gateway.turn("Inspect foo.py but leave README alone")
    assert "Controller: fail; verification: not established" in answer
    assert len(model.calls) == 1  # No extra synthesis call can erase the failure.
    assert "leave README alone" in tasks[0]
    assert "Conversation only" in gateway.turn("Why?")
    assert any("Compiler rejected" in m["content"] for m in model.calls[1][0])


def test_gateway_records_route_source_before_rules_exist():
    calls = []
    def run(task, **kwargs):
        calls.append(kwargs)
        return TaskResult("t1", "fail", "failed", False)

    gateway = ConversationGateway(
        Chat(reply("repository", "Inspect")), SimpleNamespace(run=run), EventBuffer("s")
    )
    gateway.turn("inspect repository")
    assert calls == [{"self_check": False, "route_source": RouteSource.MODEL_PROPOSAL}]

    calls.clear()
    direct = ConversationGateway(Chat(), SimpleNamespace(run=run), EventBuffer("s"))
    direct.turn("/check")
    assert calls == [{"self_check": True, "route_source": RouteSource.USER_DIRECT}]


def test_concurrent_turn_rejected_and_lock_released_after_error():
    model = Chat(reply("reply", "hello"))
    gateway = ConversationGateway(model, None, EventBuffer("s"))
    gateway._busy.acquire()
    with pytest.raises(RuntimeError, match="busy"):
        gateway.turn("hello")
    gateway._busy.release()
    assert "hello" in gateway.turn("hello")


def test_oversize_prompt_never_reaches_model():
    model = Chat()
    gateway = ConversationGateway(model, None, EventBuffer("s"), request_bytes=30)
    assert "No task was run" in gateway.turn("hello")
    assert not model.calls


def test_event_gap_and_immutable_payload_and_presenter_failure():
    def broken(event):
        raise RuntimeError("UI unavailable")
    events = EventBuffer("s", broken, capacity=2)
    payload = {"count": 1}
    first = events.emit("start", payload)
    payload["count"] = 99
    assert first.payload == {"count": 1}
    events.emit("next", {})
    events.emit("end", {})
    assert events.delivery_errors == 3
    with pytest.raises(ValueError, match="gap"):
        events.after(0)
    assert [e.sequence for e in events.after(1)] == [2, 3]


def test_controller_forces_no_mutation_even_with_permissive_repo(loaded):
    sandbox, repo, _reg, _store, _skills = loaded
    repo = replace(repo, policy=replace(repo.policy, allow_patch=True, allow_commit=True))
    controller = TaskController(repo, lambda: None, EventBuffer("s"))
    assert not controller.repo.policy.allow_patch
    assert not controller.repo.policy.allow_commit
    assert not controller.repo.policy.allow_build
    assert not controller.repo.policy.allow_test
    assert repo.policy.allow_patch  # No global/config mutation.


def test_gateway_real_controller_uses_separate_contexts_and_real_tools(loaded):
    sandbox, repo, _reg, _store, _skills = loaded
    workers = []
    def factory():
        worker = ScriptedClient([
            ChatResponse(tool_calls=[ToolCall("read", "repo_info", {})]),
            ChatResponse(tool_calls=[ToolCall("answer", "submit_answer", {
                "claim": "success", "summary": "Repository inspected.",
                "evidence_ids": ["repo_info:0"],
            })]),
        ])
        workers.append(worker)
        return worker
    events = EventBuffer("s")
    controller = TaskController(repo, factory, events)
    chat = Chat(reply("repository", "Inspect repository build configuration"),
                reply("repository", "Inspect repository build configuration again"))
    gateway = ConversationGateway(chat, controller, events)
    first = gateway.turn("Tell me how this repository builds")
    assert "Repository inspected" in first
    assert gateway.last_result.outcome == ProductOutcome.NO_VERDICT
    assert not gateway.last_result.verified_at_completion  # read != build proof
    assert gateway.last_result.evidence_ids == ("repo_info:0",)
    first_refs = gateway.last_result.evidence_refs
    first_id = gateway.last_result.task_id
    gateway.turn("Inspect again")
    assert len(workers) == 2 and workers[0] is not workers[1]
    assert gateway.last_result.task_id != first_id
    assert gateway.last_result.evidence_ids == ("repo_info:0",)
    assert gateway.last_result.evidence_refs != first_refs
    terminal = events.after(0)[-2]  # task.finished, then turn.finished
    assert terminal.task_id == gateway.last_result.task_id
    assert terminal.payload["evidence_ids"] == ["repo_info:0"]
    assert terminal.payload["route_source"] == "model_proposal"
    assert "worker.tool" in [event.kind for event in events.after(0)]


def test_execution_denial_survives_uncertain_routing(loaded):
    sandbox, repo, _reg, _store, _skills = loaded
    worker = ScriptedClient([
        ChatResponse(tool_calls=[ToolCall("execute", "build_target", {})]),
        ChatResponse(tool_calls=[ToolCall("answer", "submit_answer", {
            "claim": "needs_action", "summary": "Build was blocked.", "evidence_ids": [],
        })]),
    ])
    events = EventBuffer("s")
    controller = TaskController(repo, lambda: worker, events)
    result = controller.run("xyzzy")
    assert not result.verified_at_completion
    assert not (sandbox.root / "build").exists()


def test_check_bypasses_model_and_still_uses_controller_policy(loaded):
    sandbox, repo, _reg, _store, _skills = loaded
    model = Chat()
    events = EventBuffer("s")
    gateway = ConversationGateway(model, TaskController(repo, None, events), events)
    assert "blocked" in gateway.turn("/check")
    assert not model.calls
    started = next(e for e in events.after(0) if e.kind == "task.started")
    assert started.payload["route_source"] == "user_direct"


def test_worker_exception_is_unknown_and_presenter_does_not_get_secret(loaded):
    sandbox, repo, _reg, _store, _skills = loaded
    def broken():
        raise RuntimeError("SECRET")
    events = EventBuffer("s")
    result = TaskController(repo, broken, events).run("inspect repository")
    assert result.outcome == ProductOutcome.NO_VERDICT
    assert "SECRET" not in result.answer
    terminal = events.after(0)[-1]
    assert terminal.kind == "task.interrupted"
    assert terminal.payload == {
        "outcome": "no_verdict",
        "process_cleanup_confirmed": False,
    }