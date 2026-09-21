from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.state import AgentState
from local_agent.config import Policy, RepoConfig
from local_agent.llm.protocol import ChatResponse, ToolCall
from local_agent.llm.client import ScriptedClient
from local_agent.tools.tool_primitives import Risk, Tool, ToolRegistry, ToolResult


def _harness(tmp_path: Path):
    calls: list[dict[str, object]] = []
    repo = RepoConfig(tmp_path, "audit", "build", "runs", {}, "default", Policy())
    registry = ToolRegistry()

    def handler(profile=None, target=None):
        calls.append({"profile": profile, "target": target})
        return ToolResult(ok=True, summary="spy reached")

    registry.register(
        Tool(
            "build_target",
            "inert validation spy",
            {
                "type": "object",
                "properties": {
                    "profile": {"type": "string"},
                    "target": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler,
            Risk.READ,
        )
    )
    orch = Orchestrator(repo, registry, ScriptedClient([]), SkillLibrary([]))
    state = AgentState("audit", tmp_path, toolset=["build_target"])
    return orch, state, calls


def _dispatch(response: ChatResponse, orch: Orchestrator, state: AgentState) -> None:
    assert response.wants_tools
    for call in response.tool_calls:
        orch._execute(call, state)


def test_malformed_json_is_typed_invalid_and_never_reaches_handler(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "build_target", '{"profile":')],
        finish_reason="tool_calls",
    )

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].parse_error is not None
    _dispatch(response, orch, state)

    assert calls == []
    assert state.history[-1].reason == "invalid_model_response"


def test_non_object_json_is_rejected_before_policy_or_handler(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "build_target", "[]")],
        finish_reason="tool_calls",
    )

    _dispatch(response, orch, state)

    assert calls == []
    assert state.history[-1].reason == "invalid_model_response"


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "future_backend_value"])
def test_unqualified_terminal_never_dispatches_model_tool(tmp_path, finish_reason):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "build_target", "{}")],
        finish_reason=finish_reason,
    )

    _dispatch(response, orch, state)

    assert calls == []
    assert response.validation_errors
    assert state.history[-1].reason == "invalid_model_response"


def test_incomplete_plain_response_is_not_accepted_as_prose(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(content="partial answer", finish_reason=None)

    _dispatch(response, orch, state)

    assert calls == []
    assert state.history[-1].reason == "invalid_model_response"


def test_multiple_tool_calls_are_rejected_as_one_unqualified_batch(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[
            ToolCall.from_parts("c1", "build_target", '{}'),
            ToolCall.from_parts("c2", "build_target", '{"profile":"debug"}'),
        ],
        finish_reason="tool_calls",
    )

    _dispatch(response, orch, state)

    assert calls == []
    assert len(state.history) == 2
    assert {record.reason for record in state.history} == {"invalid_model_response"}


def test_schema_violation_is_checked_before_handler_body(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "build_target", '{"profile":7}')],
        finish_reason="tool_calls",
    )

    _dispatch(response, orch, state)

    assert calls == []
    assert state.history[-1].reason == "bad_arguments"


def test_valid_single_call_still_reaches_handler(tmp_path):
    orch, state, calls = _harness(tmp_path)
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "build_target", '{"profile":"debug"}')],
        finish_reason="tool_calls",
    )

    _dispatch(response, orch, state)

    assert calls == [{"profile": "debug", "target": None}]
    assert state.history[-1].reason is None
