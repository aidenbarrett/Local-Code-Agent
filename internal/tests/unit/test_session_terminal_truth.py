from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome, TaskResult
from local_agent.session.durable_activity import DurableToolActivity
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _admission_payload() -> dict:
    request = b"request"
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-terminal-truth",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(request).hexdigest(),
            "media_type": "application/json",
            "size_bytes": len(request),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "build-and-test",
        "execution_epoch": 0,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _submit(executor, *, request_id: str):
    return executor.submit(
        task="build it",
        request_id=request_id,
        payload_sha256="c" * 64,
        admission_payload=_admission_payload(),
        route_source="user_direct",
    )


def test_timed_out_build_cannot_claim_cleanup_not_needed(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return {"build_target", "run_test"}

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            activity = DurableToolActivity.from_task(service, task_id)
            opened = activity.start_tool("build_target")
            activity.finish_tool(
                call_id=opened.call_id,
                tool_name=opened.tool_name,
                execution="error",
                domain="unknown",
                reason="orchestrator_timeout",
                exit_code=None,
                duration_ms=1000,
            )
            return TaskResult(
                task_id,
                TaskOutcome.FAIL,
                "build timed out",
                False,
                verification_ran=False,
                reason_code="missing_evidence",
            )

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="timeout")
        assert handle.wait(5) is not None
        closed = next(event for event in service.replay() if event["kind"] == "task.closed")
        assert closed["payload"]["cleanup"] == "attempted"
        assert closed["payload"]["cleanup"] != "not_needed"
    finally:
        service.close()


def test_controller_attribute_error_is_durable_fault_and_still_raises(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return set()

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            raise AttributeError("injected controller bug")

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="fault")
        with pytest.raises(AttributeError, match="injected controller bug"):
            handle.wait(5)

        events = service.replay()
        verdict = next(event for event in events if event["kind"] == "task.verdict")
        completion = verdict["payload"]["completion"]
        assert completion["verdict_block"]["reason_code"] == "controller_fault"
        assert completion["worker_artifact_ref"] == completion["result_ref"]
        retained = service.store.artifact_bytes(completion["worker_artifact_ref"])
        assert retained is not None
        assert b"AttributeError" in retained
        assert b"injected controller bug" in retained
        assert service.store.task_record(handle.task_id)["terminal"] is True
    finally:
        service.close()


def test_open_tool_activity_forbids_completed_terminal_state(tmp_path):
    service = _service(tmp_path)

    class Controller:
        def process_spawning_tools(self):
            return set()

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            DurableToolActivity.from_task(service, task_id).start_tool("read_file")
            return TaskResult(
                task_id,
                TaskOutcome.PASS,
                "claimed complete while tool lifecycle is open",
                True,
                verification_ran=True,
                reason_code="verification_passed",
            )

    try:
        handle = _submit(CancellableDurableTaskExecutor(service, Controller()), request_id="open-tool")
        with pytest.raises(RuntimeError, match="durable tool activity remained open"):
            handle.wait(5)
        verdict = next(event for event in service.replay() if event["kind"] == "task.verdict")
        assert verdict["payload"]["completion"]["verdict_block"]["reason_code"] == "controller_fault"
        assert verdict["payload"]["completion"]["status"] == "unknown"
    finally:
        service.close()


def test_restart_recovery_records_controller_restarted(tmp_path):
    service = _service(tmp_path)
    try:
        admission = service.submit_task(
            request_id="restart",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        admission.wait(5)
        assert admission.task_id is not None

        assert service.recover_unknown_tasks() == [admission.task_id]

        verdict = next(event for event in service.replay() if event["kind"] == "task.verdict")
        closed = next(event for event in service.replay() if event["kind"] == "task.closed")
        assert verdict["payload"]["completion"]["verdict_block"]["reason_code"] == "controller_restarted"
        assert closed["payload"]["cleanup"] == "unknown"
    finally:
        service.close()


def _completed_after_failed_process_tool(service, *, process_started, execution="blocked",
                                         reason="policy_denied"):
    """A controller whose worker hit one failed run_test and then completed."""

    class Controller:
        def process_spawning_tools(self):
            return {"build_target", "run_test"}

        def run(self, task, *, self_check=False, route_source=None, task_id=None, skill_name=None):
            activity = DurableToolActivity.from_task(service, task_id)
            opened = activity.start_tool("run_test")
            activity.finish_tool(
                call_id=opened.call_id,
                tool_name=opened.tool_name,
                execution=execution,
                domain="unknown",
                reason=reason,
                exit_code=None,
                duration_ms=1,
                failure_detail="running tests is disabled by repository policy",
                process_started=process_started,
            )
            return TaskResult(
                task_id,
                TaskOutcome.PASS,
                "answered without running tests",
                True,
                verification_ran=True,
                reason_code="verification_passed",
            )

    return Controller()


def test_process_tool_refused_before_spawning_does_not_block_a_completed_terminal(tmp_path):
    """gpu30b-2 J10.r3: a run_test that started nothing made the terminal unwritable."""
    service = _service(tmp_path)
    try:
        controller = _completed_after_failed_process_tool(service, process_started=False)
        handle = _submit(CancellableDurableTaskExecutor(service, controller), request_id="refused")
        assert handle.wait(5) is not None
        closed = next(event for event in service.replay() if event["kind"] == "task.closed")
        assert closed["payload"]["status"] == "completed"
        assert closed["payload"]["cleanup"] == "not_needed"
    finally:
        service.close()


@pytest.mark.parametrize("process_started", [True, None])
def test_failed_process_tool_without_proof_of_no_spawn_keeps_cleanup_unknown(
        tmp_path, process_started):
    """A process that may have started, or an unrecorded one, is never cleanup not_needed."""
    service = _service(tmp_path)
    try:
        controller = _completed_after_failed_process_tool(
            service, process_started=process_started, execution="error", reason="internal_error",
        )
        handle = _submit(CancellableDurableTaskExecutor(service, controller), request_id="maybe")
        # A verified result whose cleanup is unknown closes honestly, with no verdict,
        # instead of failing the terminal write and leaving no terminal at all.
        assert handle.wait(5) is not None
        events = service.replay()
        closed = next(event for event in events if event["kind"] == "task.closed")
        completion = next(e for e in events if e["kind"] == "task.verdict")["payload"]["completion"]
        assert closed["payload"]["status"] == "unknown"
        assert closed["payload"]["cleanup"] == "unknown"
        assert completion["verdict_block"]["verdict"] == "NO_VERDICT"
        assert completion["verdict_block"]["reason_code"] == "cleanup_unknown"
        retained = service.store.artifact_bytes(completion["result_ref"]).decode("utf-8")
        assert "may still be running" in retained
        assert "answered without running tests" in retained  # the worker's answer is kept
        assert service.store.task_record(handle.task_id)["terminal"] is True
    finally:
        service.close()


def test_only_a_completed_result_with_unreconciled_cleanup_is_downgraded():
    from local_agent.session.cancellable_task_executor import _with_reconciled_cleanup

    task_id = str(uuid4())
    passed = TaskResult(task_id, TaskOutcome.PASS, "done", True, verification_ran=True,
                        reason_code="verification_passed")
    failed = TaskResult(task_id, TaskOutcome.FAIL, "broken", False, verification_ran=True,
                        reason_code="verification_failed")
    for cleanup in ("not_needed", "confirmed"):
        assert _with_reconciled_cleanup(passed, cleanup) is passed
    for cleanup in ("unknown", "attempted"):
        assert _with_reconciled_cleanup(failed, cleanup) is failed
        downgraded = _with_reconciled_cleanup(passed, cleanup)
        assert downgraded.outcome == TaskOutcome.NO_VERDICT  # value: other tests reload the module
        assert downgraded.reason_code == "cleanup_unknown"
        assert downgraded.verified_at_completion is False
        assert downgraded.answer.endswith("done")


@pytest.mark.parametrize("effect", ["apply", "commit"])
@pytest.mark.parametrize("after_effect", [False, True], ids=["before", "after"])
def test_restart_at_candidate_effect_boundary_never_replays_or_claims_success(
    tmp_path, effect, after_effect,
):
    """Model abrupt death by leaving admission durable but no terminal result.

    Effects use the real Git workspace owner. Reopening/recovery must neither
    replay the write nor infer completion from the checkout's current state.
    """
    from test_session_workspaces import _git, _manager, _state, _user_repo

    user = _user_repo(tmp_path)
    _git(user, "config", "user.email", "restart@example.invalid")
    _git(user, "config", "user.name", "Restart test")
    manager = _manager(tmp_path)
    candidate_id = str(uuid4())
    workspace = manager.create(user, candidate_id)
    try:
        source = user / "src" / "a.cpp"
        original = source.read_bytes()
        (workspace.root / "src" / "a.cpp").write_bytes(b"int a() { return 42; }\n")
        candidate = manager.candidate_patch(workspace)
        if effect == "commit":
            imported = manager.import_patch(workspace, candidate)
            assert imported.applied and imported.verified
            manager.record_applied(candidate_id, user, candidate, imported)
        service = _service(tmp_path)
        stream_id, session_id = service.stream_id, service.session_id
        admission = service.submit_task(
            request_id=f"{effect}-boundary",
            payload_sha256="c" * 64,
            admission_payload=_admission_payload(),
        )
        admission.wait(5)
        task_id = admission.task_id
        assert task_id is not None
        head_before = _git(user, "rev-parse", "HEAD")
        if after_effect:
            if effect == "apply":
                # Death after writing, before even recording an applied receipt.
                imported = manager.import_patch(workspace, candidate)
                assert imported.applied and imported.verified
            else:
                manager.commit_applied(candidate_id, user, "approved candidate")
        head_at_death = _git(user, "rev-parse", "HEAD")
        if effect == "commit":
            assert (head_at_death != head_before) is after_effect
        else:
            assert (source.read_bytes() != original) is after_effect
        state_at_death = _state(user)
        bytes_at_death = source.read_bytes()
        service.close()  # no task finalization: the process has disappeared

        reopened = DurableSessionService(
            SQLiteSessionStore(tmp_path / "session.db"),
            stream_id=stream_id, session_id=session_id,
        )
        try:
            assert reopened.recover_unknown_tasks() == [task_id]
            assert reopened.recover_unknown_tasks() == []
            assert _git(user, "rev-parse", "HEAD") == head_at_death
            assert _state(user) == state_at_death
            assert source.read_bytes() == bytes_at_death
            events = reopened.replay()
            verdicts = [event for event in events if event["kind"] == "task.verdict"]
            assert len(verdicts) == 1
            completion = verdicts[0]["payload"]["completion"]
            assert completion["status"] == "unknown"
            assert completion["verdict_block"]["verdict"] == "NO_VERDICT"
            assert completion["verdict_block"]["reason_code"] == "controller_restarted"
            retained = reopened.store.artifact_bytes(completion["result_ref"])
            assert retained is not None
            assert b'"verified_at_completion":false' in retained
            assert b"cleanup is unknown" in retained
            assert any("not retried" in line for line in
                       completion["verdict_block"]["rendered_lines"])
        finally:
            reopened.close()
    finally:
        manager.close(workspace)
