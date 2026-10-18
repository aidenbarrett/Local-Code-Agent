"""Tool-registry adapter that makes durable activity part of the effect boundary.

The durable Session Hub contract requires ``tool.started`` to commit before an
effectful handler runs. This adapter returns a fresh registry whose handlers are
wrapped by one already-admitted ``DurableToolActivity``. It does not change tool
schemas, risk, policy, or result semantics; the orchestrator remains responsible
for deciding whether a tool may be invoked.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from time import monotonic


from ..tools.tool_primitives import (
    BlockedError,
    CommandStoppedError,
    Reason,
    Risk,
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
)


def _finish_exception(
    finish: Callable[..., None], exc: BaseException, started: float,
    *, settled_before_stop: bool = False,
) -> None:
    """Close one call as a typed failure; ``finish`` already names the call.

    ``settled_before_stop`` is the controller's own count saying that a Stop fenced
    this call after every process it had spawned was accounted ended by the runner.
    """
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
    # Only a stopped call has a cleanup answer to record: the runner's typed answer for
    # an interrupted command, or the settled-process count for a call whose earlier
    # steps all ended before Stop fenced its next one.
    extra: dict[str, bool | None] = {}
    if isinstance(exc, CommandStoppedError):
        extra = {"process_cleanup_confirmed": exc.process_cleanup_confirmed}
    elif (settled_before_stop and isinstance(exc, BlockedError)
            and exc.reason is Reason.COMMAND_CANCELLED):
        extra = {"process_cleanup_confirmed": True}
    finish(
        execution=execution,
        domain="unknown",
        reason=reason,
        exit_code=None,
        duration_ms=max(0, int((monotonic() - started) * 1000)),
        failure_detail=str(exc),
        **extra,
    )


def wrap_registry_with_durable_activity(
    registry: ToolRegistry,
    activity,
    *,
    process_starts: Callable[[], int] | None = None,
    process_settlements: Callable[[], int] | None = None,
) -> ToolRegistry:
    """Return an equivalent registry whose handler effects are durably bracketed.

    Schema validation on the returned ``Tool`` still runs before this wrapper.
    A rejected call is recorded without entering the effectful handler.
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

        def rejected(exc: ToolError, *, _tool: Tool = original) -> None:
            opened = activity.start_tool(_tool.name)
            _finish_exception(partial(
                activity.finish_tool, call_id=opened.call_id, tool_name=_tool.name,
                process_started=False if _tool.risk is Risk.EXECUTE else None,
            ), exc, monotonic())

        def handler(*, _tool=original, **kwargs):
            opened = activity.start_tool(_tool.name)
            started = monotonic()
            counter = process_starts if _tool.risk is Risk.EXECUTE else None
            spawned_before = counter() if counter is not None else 0
            settled = process_settlements if counter is not None else None
            settled_before = settled() if settled is not None else 0

            def process_started() -> bool | None:
                # Only a tool that can spawn has a meaningful answer; for the rest the
                # question does not arise and the record says so with None.
                return None if counter is None else counter() > spawned_before

            def all_settled() -> bool:
                # Every process this call spawned was accounted ended by the runner.
                if counter is None or settled is None:
                    return False
                spawned = counter() - spawned_before
                return spawned > 0 and settled() - settled_before == spawned

            def fail(exc: BaseException) -> None:
                _finish_exception(
                    partial(
                        activity.finish_tool,
                        call_id=opened.call_id,
                        tool_name=_tool.name,
                        process_started=process_started(),
                    ),
                    exc,
                    started,
                    settled_before_stop=all_settled(),
                )

            try:
                result = _tool.handler(**kwargs)
            except BaseException as exc:
                fail(exc)
                raise
            if not isinstance(result, ToolResult):
                exc = RuntimeError(
                    f"tool {_tool.name!r} returned {type(result).__name__}, not ToolResult"
                )
                fail(exc)
                raise exc
            try:
                activity.finish_tool(
                    call_id=opened.call_id,
                    tool_name=_tool.name,
                    execution=result.execution_status.value,
                    domain=result.domain_status.value,
                    reason=result.reason.value if result.reason is not None else None,
                    exit_code=result.exit_code,
                    duration_ms=max(0, int((monotonic() - started) * 1000)),
                    failure_detail=None if result.ran else result.summary,
                    process_started=None if result.ran else process_started(),
                    proposal_data=dict(result.data)
                    if result.ok and _tool.name in {"propose_patch", "propose_file"} else None,
                )
            except Exception as exc:
                # The result could not be recorded as typed (a contract rejection, or
                # the writer failing). Close this exact call as an internal error so
                # the next tool is not refused for an open call this one left behind,
                # then surface the original failure. If closing fails too, that
                # failure propagates with this one chained.
                fail(exc)
                raise
            return result

        wrapped.register(
            Tool(
                name=original.name,
                description=original.description,
                parameters=original.parameters,
                handler=handler,
                risk=original.risk,
                on_validation_error=rejected,
            )
        )
    return wrapped
