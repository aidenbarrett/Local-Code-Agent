"""Tool-registry adapter that makes durable activity part of the effect boundary.

The durable Session Hub contract requires ``tool.started`` to commit before an
effectful handler runs. This adapter returns a fresh registry whose handlers are
wrapped by one already-admitted ``DurableToolActivity``. It does not change tool
schemas, risk, policy, or result semantics; the orchestrator remains responsible
for deciding whether a tool may be invoked.
"""
from __future__ import annotations

from time import monotonic

from ..tools.tool_primitives import (
    BlockedError,
    Reason,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
)


def _finish_exception(activity, opened, tool_name: str, exc: BaseException, started: float) -> None:
    if isinstance(exc, BlockedError):
        execution = "blocked"
        reason = exc.reason.value
    elif isinstance(exc, ToolError):
        execution = "error"
        reason = getattr(exc, "reason", Reason.BAD_ARGUMENTS).value
    elif isinstance(exc, TypeError):
        execution = "error"
        reason = Reason.BAD_ARGUMENTS.value
    else:
        execution = "error"
        reason = Reason.INTERNAL_ERROR.value
    activity.finish_tool(
        call_id=opened.call_id,
        tool_name=tool_name,
        execution=execution,
        domain="unknown",
        reason=reason,
        exit_code=None,
        duration_ms=max(0, int((monotonic() - started) * 1000)),
    )


def wrap_registry_with_durable_activity(
    registry: ToolRegistry,
    activity,
) -> ToolRegistry:
    """Return an equivalent registry whose handler effects are durably bracketed.

    Schema validation on the returned ``Tool`` still runs before this wrapper, so
    malformed arguments cannot create a durable start event or reach the handler.
    If durable start itself fails, the underlying handler is never called. Handler
    exceptions are durably classified and then re-raised unchanged so the existing
    orchestrator retains its normal error semantics.
    """
    if not isinstance(registry, ToolRegistry):
        raise TypeError("durable tool wrapping requires a ToolRegistry")
    if not callable(getattr(activity, "start_tool", None)) or not callable(
        getattr(activity, "finish_tool", None)
    ):
        raise TypeError("durable tool wrapping requires a DurableToolActivity-like value")

    wrapped = ToolRegistry()
    for name in registry.names():
        original = registry.get(name)

        def handler(*, _tool=original, **kwargs):
            opened = activity.start_tool(_tool.name)
            started = monotonic()
            try:
                result = _tool.handler(**kwargs)
            except BaseException as exc:
                _finish_exception(activity, opened, _tool.name, exc, started)
                raise
            if not isinstance(result, ToolResult):
                exc = TypeError(f"tool {_tool.name!r} returned {type(result).__name__}, not ToolResult")
                _finish_exception(activity, opened, _tool.name, exc, started)
                raise exc
            activity.finish_tool(
                call_id=opened.call_id,
                tool_name=_tool.name,
                execution=result.execution_status.value,
                domain=result.domain_status.value,
                reason=result.reason.value if result.reason is not None else None,
                exit_code=result.exit_code,
                duration_ms=max(0, int((monotonic() - started) * 1000)),
            )
            return result

        wrapped.register(
            Tool(
                name=original.name,
                description=original.description,
                parameters=original.parameters,
                handler=handler,
                risk=original.risk,
            )
        )
    return wrapped