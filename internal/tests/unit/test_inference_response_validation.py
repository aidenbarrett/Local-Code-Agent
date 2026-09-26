from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.state import AgentState
from local_agent.config import Policy, RepoConfig
from local_agent.llm.protocol import CallStats, ChatResponse, ToolCall
from local_agent.llm.client import ScriptedClient
from local_agent.tools.tool_primitives import Risk, Tool, ToolRegistry, ToolResult


REPO = Path(__file__).resolve().parent.parent.parent


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


def test_operational_qualification_rejects_malformed_unoffered_tool():
    serving = REPO / "serving"
    if str(serving) not in sys.path:
        sys.path.insert(0, str(serving))

    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    class InvalidToolServer:
        def __init__(self):
            self._seen: set[str] = set()

        def probe(self):
            return {"served_models": ["m"], "configured_model_present": True}

        def chat(self, messages, tools=None, max_tokens=None):
            text = "".join(str(m.get("content") or "") for m in messages)
            key = text + ("|tools" if tools else "")
            prompt_tokens = max(1, len(text) // 4)
            cached = max(0, prompt_tokens - 1) if key in self._seen else 0
            self._seen.add(key)
            names = {t["function"]["name"] for t in (tools or [])}
            if names == {"submit_answer"}:
                calls = [ToolCall.from_parts(
                    "finish", "submit_answer", '{"claim":"diagnosis","summary":"done"}'
                )]
                content = ""
            elif names:
                calls = [ToolCall.from_parts("bad", "nonexistent_tool", "{not json")]
                content = ""
            else:
                calls = []
                content = "pomegranate READY"
            return ChatResponse(
                content=content,
                tool_calls=calls,
                finish_reason="tool_calls" if calls else "stop",
                stats=CallStats(
                    total_s=0.5,
                    ttft_s=0.03,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=8,
                    cached_tokens=cached,
                    streamed=True,
                    thinking_requested=False,
                    thinking_control_accepted=True,
                ),
            )

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "fixture",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_answer",
                "description": "fixture",
                "parameters": {
                    "type": "object",
                    "properties": {"claim": {"type": "string"}, "summary": {"type": "string"}},
                    "required": ["claim", "summary"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    q = run_qualification(
        InvalidToolServer(), MODEL_PRESETS["ptl-npu-8b"], schemas, [1000]
    )
    check = next(c for c in q.checks if c.name == "tool call round trip (streaming)")

    assert check.status == "FAIL"
    assert "validation failed" in check.detail or "unoffered tool" in check.detail
