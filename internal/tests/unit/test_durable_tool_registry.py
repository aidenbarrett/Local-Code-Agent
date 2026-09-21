from __future__ import annotations

from dataclasses import dataclass

import pytest

from local_agent.session.durable_tool_registry import wrap_registry_with_durable_activity
from local_agent.tools.tool_primitives import (
    BlockedError,
    DomainStatus,
    Reason,
    Risk,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
)


@dataclass(frozen=True)
class _Opened:
    call_id: str


class _Activity:
    def __init__(self, *, fail_start: bool = False):
        self.fail_start = fail_start
        self.started = []
        self.finished = []

    def start_tool(self, name):
        if self.fail_start:
            raise RuntimeError("durable writer unavailable")
        self.started.append(name)
        return _Opened(f"call-{len(self.started)}")

    def finish_tool(self, **payload):
        self.finished.append(payload)


def _registry(handler):
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="demo",
            description="fixture tool",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=handler,
            risk=Risk.EXECUTE,
        )
    )
    return registry


def test_durable_start_commits_before_handler_effect_and_typed_finish_follows():
    order = []
    activity = _Activity()

    original_start = activity.start_tool
    original_finish = activity.finish_tool

    def start(name):
        order.append("durable-start")
        return original_start(name)

    def finish(**payload):
        order.append("durable-finish")
        original_finish(**payload)

    activity.start_tool = start
    activity.finish_tool = finish

    def handler(value):
        order.append("effect")
        return ToolResult(
            ok=False,
            summary=f"observed {value}",
            exit_code=7,
            domain_status=DomainStatus.FAIL,
        )

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    result = wrapped.get("demo").handler(value=3)

    assert result.exit_code == 7
    assert order == ["durable-start", "effect", "durable-finish"]
    assert activity.finished[0]["execution"] == "ok"
    assert activity.finished[0]["domain"] == "fail"
    assert activity.finished[0]["reason"] is None
    assert activity.finished[0]["exit_code"] == 7


def test_failed_durable_start_prevents_handler_effect():
    effects = []
    activity = _Activity(fail_start=True)

    def handler(value):
        effects.append(value)
        return ToolResult(True, "ok")

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(RuntimeError, match="durable writer unavailable"):
        wrapped.get("demo").handler(value=1)

    assert effects == []
    assert activity.finished == []


def test_schema_rejection_happens_before_durable_start_and_effect():
    effects = []
    activity = _Activity()

    def handler(value):
        effects.append(value)
        return ToolResult(True, "ok")

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(ToolError, match="violate its schema"):
        wrapped.get("demo").handler(value="wrong-type")

    assert activity.started == []
    assert activity.finished == []
    assert effects == []


def test_blocked_handler_is_durably_finished_then_re_raised_unchanged():
    activity = _Activity()
    error = BlockedError("missing toolchain", Reason.MISSING_EXECUTABLE)

    def handler(value):
        raise error

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(BlockedError) as caught:
        wrapped.get("demo").handler(value=1)

    assert caught.value is error
    assert activity.started == ["demo"]
    assert activity.finished[0]["execution"] == "blocked"
    assert activity.finished[0]["domain"] == "unknown"
    assert activity.finished[0]["reason"] == "missing_executable"


def test_invalid_tool_return_is_recorded_as_internal_error():
    activity = _Activity()

    def handler(value):
        return {"ok": True}

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(RuntimeError, match="not ToolResult"):
        wrapped.get("demo").handler(value=1)

    assert activity.finished[0]["execution"] == "error"
    assert activity.finished[0]["domain"] == "unknown"
    assert activity.finished[0]["reason"] == "internal_error"
