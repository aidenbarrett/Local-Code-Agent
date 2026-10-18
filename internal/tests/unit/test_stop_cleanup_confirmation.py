"""A Stop is reconciled only by the command runner's typed cleanup answer.

When Stop ends a running command, the runner says whether the command's whole process
tree is gone (Linux subreaper, Windows Job settlement). That answer is retained as a
typed field of the call's failure record, never parsed from its message, and terminal
truth reads `confirmed` only when every spawned tree either ended on its own or was
stopped with that confirmation. Anything less stays `attempted` or `unknown`.
"""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from local_agent.session.durable_activity import DurableToolActivity, tool_failure_artifact
from local_agent.session.durable_tool_registry import _finish_exception
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.terminal_truth import derive_terminal_activity_truth
from local_agent.tools.tool_primitives import CommandStoppedError, ToolError

SPAWNING = frozenset({"build_target", "run_test"})


def _admitted(tmp_path):
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    body = b"request"
    receipt = service.submit_task(
        request_id="request-1", payload_sha256="c" * 64,
        admission_payload={
            "origin": {"kind": "user_direct", "turn_ref": {
                "conversation_id": "c", "turn_index": 0, "turn_sha256": "a" * 64}},
            "request_ref": {"artifact_id": str(uuid4()), "sha256": hashlib.sha256(body).hexdigest(),
                            "media_type": "application/json", "size_bytes": len(body),
                            "availability": "unavailable"},
            "contract_sha256": "b" * 64, "repository_id": "repo-1", "skill": "run-build",
            "execution_epoch": 0, "deadline_utc": "2030-01-01T00:00:00Z",
        })
    receipt.wait(5)
    return service, receipt.task_id


def _stopped(activity, tool: str, confirmed: bool | None) -> None:
    opened = activity.start_tool(tool)
    activity.finish_tool(
        call_id=opened.call_id, tool_name=tool, execution="error", domain="unknown",
        reason="command_cancelled", exit_code=None, duration_ms=5,
        failure_detail="Stop interrupted this command.", process_started=True,
        process_cleanup_confirmed=confirmed,
    )


def _ended(activity, tool: str) -> None:
    opened = activity.start_tool(tool)
    activity.finish_tool(call_id=opened.call_id, tool_name=tool, execution="ok", domain="pass",
                         reason=None, exit_code=0, duration_ms=5)


def _cleanup(service, task_id) -> tuple[str, tuple[str, ...]]:
    truth = derive_terminal_activity_truth(service, task_id=task_id, execution_epoch=0,
                                           process_spawning_tools=SPAWNING)
    return truth.cleanup, truth.open_call_ids


@pytest.mark.parametrize(("calls", "expected"), [
    ((("stop", True),), "confirmed"),
    ((("end", None), ("stop", True)), "confirmed"),
    ((("stop", False),), "attempted"),
    ((("stop", None),), "attempted"),
    ((("stop", True), ("stop", False)), "attempted"),
])
def test_only_a_typed_confirmation_for_every_stopped_tree_reconciles_cleanup(tmp_path, calls, expected):
    service, task_id = _admitted(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        for kind, confirmed in calls:
            if kind == "end":
                _ended(activity, "build_target")
            else:
                _stopped(activity, "build_target", confirmed)
        assert _cleanup(service, task_id) == (expected, ())
    finally:
        service.close()


def test_a_still_open_command_is_never_confirmed(tmp_path):
    service, task_id = _admitted(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        _stopped(activity, "build_target", True)
        opened = activity.start_tool("run_test")
        assert _cleanup(service, task_id) == ("unknown", (opened.call_id,))
    finally:
        service.close()


def test_a_message_claiming_confirmation_is_not_a_confirmation(tmp_path, monkeypatch):
    """A record older than the typed field, whatever its prose says, confirms nothing."""
    from local_agent.session import durable_activity

    def legacy(**_kwargs):
        body = json.dumps({"schema": "lca.tool-failure/2", "tool_name": "build_target",
                           "tool_reason": "command_cancelled", "process_started": True,
                           "detail": "Process-tree cleanup confirmed=true"}).encode()
        return ({"artifact_id": str(uuid4()), "sha256": hashlib.sha256(body).hexdigest(),
                 "media_type": "application/json", "size_bytes": len(body),
                 "availability": "retained"}, body)

    monkeypatch.setattr(durable_activity, "tool_failure_artifact", legacy)
    service, task_id = _admitted(tmp_path)
    try:
        _stopped(DurableToolActivity.from_task(service, task_id), "build_target", None)
        assert _cleanup(service, task_id)[0] == "attempted"
    finally:
        service.close()


def test_the_runners_answer_reaches_the_record_typed():
    seen: dict = {}
    _finish_exception(lambda **kw: seen.update(kw),
                      CommandStoppedError("stopped", process_cleanup_confirmed=True), 0.0)
    assert seen["process_cleanup_confirmed"] is True and seen["reason"] == "command_cancelled"
    seen.clear()
    _finish_exception(lambda **kw: seen.update(kw), ToolError("other"), 0.0)
    assert "process_cleanup_confirmed" not in seen
    _, body = tool_failure_artifact(tool_name="build_target", tool_reason="command_cancelled",
                                    failure_detail="x", process_started=True,
                                    process_cleanup_confirmed=True)
    record = json.loads(body)
    assert record["schema"] == "lca.tool-failure/3" and record["process_cleanup_confirmed"] is True


def test_a_reconciled_stop_reads_as_stopped_and_an_unreconciled_one_does_not():
    from local_agent.session.result_presentation import (
        render_result_next_action,
        render_result_summary,
    )
    from local_agent.session.task_read_model import TaskSnapshot

    def task(reason: str) -> TaskSnapshot:
        return TaskSnapshot(
            task_id=str(uuid4()), admitted_sequence=1, last_sequence=5, state="unknown",
            execution_epoch=0, origin_kind="user_rule", repository_id="repo-1",
            skill="run-build", deadline_utc="2030-01-01T00:00:00Z", verdict="NO_VERDICT",
            verdict_reason=reason, closed_sequence=5,
        )

    stopped = task("cancelled")
    assert render_result_summary(stopped).startswith("Result: NO VERDICT — stopped on request")
    assert "confirmed ended" in render_result_summary(stopped)
    assert render_result_next_action(stopped) == (
        "Next action: repeat the request when you want a result.")
    unreconciled = task("cancel_unreconciled")
    assert "cleanup is not fully reconciled" in render_result_summary(unreconciled)
    assert "cleanup state" in render_result_next_action(unreconciled)


class _StopAfterFirstStep:
    """Stop arrives after the first configured command returned, before the next one."""

    def __init__(self) -> None:
        self.returned = 0

    @property
    def requested(self) -> bool:
        return self.returned >= 1


def _two_step_build(tmp_path, service, task_id, monkeypatch, *, first_unproven=False):
    import sys

    from local_agent.config import load_repo_config
    from local_agent.session.durable_tool_registry import wrap_registry_with_durable_activity
    from local_agent.tools import build_registry
    from local_agent.tools.tool_primitives import BlockedError

    root = tmp_path / "repo"
    root.mkdir()
    ok = json.dumps([sys.executable, "-c", "raise SystemExit(0)"])
    (root / ".local-agent.toml").write_text(
        f"[profiles.debug]\nconfigure={ok}\nbuild={ok}\n", encoding="utf-8")
    probe = _StopAfterFirstStep()
    registry, ctx, _ = build_registry(load_repo_config(root), cancellation_probe=probe)
    from local_agent.tools import tool_context

    real = tool_context.run_command

    def counted(*args, **kwargs):
        outcome = real(*args, **kwargs)
        if first_unproven:
            outcome.process_cleanup_confirmed = False
        probe.returned += 1
        return outcome

    monkeypatch.setattr(tool_context, "run_command", counted)
    activity = DurableToolActivity.from_task(service, task_id)
    wrapped = wrap_registry_with_durable_activity(
        registry, activity,
        process_starts=lambda: ctx.processes_started,
        process_settlements=lambda: ctx.processes_settled,
    )
    with pytest.raises(BlockedError):
        wrapped.get("build_target").handler()
    return ctx


def test_stop_between_configured_steps_is_confirmed_by_the_settled_count(tmp_path, monkeypatch):
    """Configure ran and ended; Stop then fenced the build before it spawned. Nothing
    is left running, and terminal truth says so from typed counts, not prose."""
    service, task_id = _admitted(tmp_path)
    try:
        ctx = _two_step_build(tmp_path, service, task_id, monkeypatch)
        assert (ctx.processes_started, ctx.processes_settled) == (1, 1)
        assert _cleanup(service, task_id) == ("confirmed", ())
    finally:
        service.close()


def test_stop_after_a_step_whose_cleanup_is_unproven_stays_attempted(tmp_path, monkeypatch):
    service, task_id = _admitted(tmp_path)
    try:
        ctx = _two_step_build(tmp_path, service, task_id, monkeypatch, first_unproven=True)
        assert ctx.processes_settled == 0
        assert _cleanup(service, task_id) == ("attempted", ())
    finally:
        service.close()


def test_settled_count_only_confirms_a_stop_fence():
    from local_agent.tools.tool_primitives import BlockedError, Reason

    seen: dict = {}
    _finish_exception(lambda **kw: seen.update(kw),
                      BlockedError("policy", Reason.BAD_ARGUMENTS), 0.0,
                      settled_before_stop=True)
    assert "process_cleanup_confirmed" not in seen
    seen.clear()
    _finish_exception(lambda **kw: seen.update(kw),
                      BlockedError("stop", Reason.COMMAND_CANCELLED), 0.0,
                      settled_before_stop=False)
    assert "process_cleanup_confirmed" not in seen
    _finish_exception(lambda **kw: seen.update(kw),
                      BlockedError("stop", Reason.COMMAND_CANCELLED), 0.0,
                      settled_before_stop=True)
    assert seen["process_cleanup_confirmed"] is True
