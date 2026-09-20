import json

from local_agent.agent.context import ContextManager, tool_result_message
from local_agent.llm.client import _transport_kind
from local_agent.tools.tool_primitives import ToolResult


def test_request_budget_includes_tool_schema_overhead_and_server_calibration():
    ctx = ContextManager(budget_tokens=7500)
    ctx.append({"role": "system", "content": "x" * 4000})
    visible = ctx.tokens
    ctx.set_tool_schemas([
        {"type": "function", "function": {"name": "list_files", "parameters": {"type": "object", "description": "y" * 2400}}}
    ])
    assert ctx.estimated_request_tokens > visible

    before = ctx.tokens
    ctx.observe_prompt_tokens(before + 1700, before)
    assert ctx.request_overhead_tokens >= 1828
    assert ctx.estimated_request_tokens == ctx.tokens + ctx.request_overhead_tokens


def test_compaction_can_collapse_recent_large_tool_result_to_avoid_overflow():
    ctx = ContextManager(budget_tokens=1800, keep_recent_tool_results=6, hysteresis=0.7)
    ctx.set_tool_schemas([{"name": "list_files", "description": "schema" * 80}])
    ctx.append({"role": "system", "content": "rules" * 100})
    ctx.append({"role": "user", "content": "summarize repo"})
    payload = json.dumps({"ok": True, "summary": "100 files", "data": {"files": [f"internal/path/{i}/file.cpp" for i in range(400)]}})
    ctx.append(tool_result_message("call_1", "list_files", payload))
    assert ctx.needs_compaction()
    event = ctx.compact()
    assert event is not None
    assert event.collapsed_results == 1
    assert json.loads(ctx.messages[-1]["content"])["collapsed"] is True
    assert ctx.estimated_request_tokens < event.tokens_before


def test_model_tool_result_token_budget_can_drop_oversized_payload_without_artifact():
    result = ToolResult(True, "100 files", data={"files": ["x" * 100 for _ in range(100)]})
    rendered = json.loads(result.to_json(max_bytes=800))
    assert rendered["truncated"] is True
    assert rendered["artifacts"] == []
    assert "smaller limit" in rendered["note"]


def test_prompt_length_bad_request_is_not_classified_as_server_unavailable():
    exc = RuntimeError("400: Input length exceeds the maximum allowed length")
    assert _transport_kind(exc) == "context_overflow"
