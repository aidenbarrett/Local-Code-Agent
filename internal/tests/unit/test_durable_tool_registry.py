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


def test_a_result_the_contract_rejects_closes_its_call_instead_of_jamming_the_task():
    # The real durable writer refuses the typed finish; the wrapper must still close
    # this exact call, or every later tool fails with "a previous tool call is still open".
    from local_agent.session.durable_activity import DurableActivityError

    class _Refuses(_Activity):
        def finish_tool(self, **payload):
            if payload["execution"] == "ok":
                raise DurableActivityError("contract rejects this finish")
            super().finish_tool(**payload)

    activity = _Refuses()

    def handler(value):
        return ToolResult(ok=True, summary="ran", reason=Reason.POLICY_DENIED)

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(DurableActivityError):
        wrapped.get("demo").handler(value=1)
    assert [(f["execution"], f["reason"]) for f in activity.finished] == [("error", "internal_error")]


def test_raised_tool_error_keeps_its_typed_reason_and_message_as_failure_detail():
    activity = _Activity()

    def handler(value):
        raise ToolError(f"'src/missing_{value}.cpp' is not a file", reason=Reason.NOT_FOUND)

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    with pytest.raises(ToolError):
        wrapped.get("demo").handler(value=4)

    finished = activity.finished[0]
    assert finished["execution"] == "error"
    assert finished["reason"] == "not_found"
    assert finished["failure_detail"] == "'src/missing_4.cpp' is not a file"


def test_cleanly_executed_result_carries_no_failure_detail_even_when_it_fails():
    activity = _Activity()

    def handler(value):
        return ToolResult(ok=False, summary="3 tests failed", domain_status=DomainStatus.FAIL)

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    wrapped.get("demo").handler(value=1)

    assert activity.finished[0]["execution"] == "ok"
    assert activity.finished[0]["failure_detail"] is None


def test_errored_result_keeps_its_summary_as_failure_detail():
    activity = _Activity()

    def handler(value):
        return ToolResult.errored(Reason.BAD_ARGUMENTS, "end_line must be greater than or equal to start_line")

    wrapped = wrap_registry_with_durable_activity(_registry(handler), activity)
    wrapped.get("demo").handler(value=1)

    assert activity.finished[0]["reason"] == "bad_arguments"
    assert activity.finished[0]["failure_detail"] == (
        "end_line must be greater than or equal to start_line"
    )


def _execute_registry(handler):
    registry = ToolRegistry()
    registry.register(Tool(
        name="run_test", description="fixture", parameters={
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False},
        handler=handler, risk=Risk.EXECUTE))
    return registry


def test_execute_tool_refused_before_spawning_records_no_process_started():
    activity = _Activity()
    spawned = [0]

    def handler(value):
        raise ToolError("unknown build profile 'debug\\n<parameter=name_filter>'")

    wrapped = wrap_registry_with_durable_activity(
        _execute_registry(handler), activity, process_starts=lambda: spawned[0])
    with pytest.raises(ToolError):
        wrapped.get("run_test").handler(value=1)
    assert activity.finished[0]["process_started"] is False


def test_execute_tool_that_spawned_then_failed_records_process_started():
    activity = _Activity()
    spawned = [0]

    def handler(value):
        spawned[0] += 1
        raise RuntimeError("runner broke after spawn")

    wrapped = wrap_registry_with_durable_activity(
        _execute_registry(handler), activity, process_starts=lambda: spawned[0])
    with pytest.raises(RuntimeError):
        wrapped.get("run_test").handler(value=1)
    assert activity.finished[0]["process_started"] is True


def test_non_execute_or_uncounted_calls_record_no_process_fact():
    def handler(value):
        raise ToolError("nope")

    read_only = ToolRegistry()
    read_only.register(Tool(
        name="read_file", description="fixture", parameters={
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False},
        handler=handler, risk=Risk.READ))
    for registry, counter in ((read_only, lambda: 0), (_execute_registry(handler), None)):
        activity = _Activity()
        wrapped = wrap_registry_with_durable_activity(registry, activity, process_starts=counter)
        name = registry.names()[0]
        with pytest.raises(ToolError):
            wrapped.get(name).handler(value=1)
        assert activity.finished[0]["process_started"] is None
