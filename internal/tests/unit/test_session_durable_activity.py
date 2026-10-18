from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from local_agent.agent.orchestrator import Orchestrator
from local_agent.agent.skills import SkillLibrary
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellation import CancellationToken
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.task_controller import TaskController
from local_agent.session.workspaces import GitWorkspaceManager
from local_agent.tools import build_registry
from local_agent.session.durable_tool_registry import wrap_registry_with_durable_activity

from local_agent.session.durable_activity import (
    DurableActivityError,
    DurableToolActivity,
    durable_tool_reason,
    tool_failure_artifact,
)
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import (
    ArtifactIntegrityError,
    SQLiteSessionStore,
    TaskStateConflict,
)


def _admission_payload(*, execution_epoch: int = 0) -> dict:
    payload = b"request"
    return {
        "origin": {
            "kind": "user_direct",
            "turn_ref": {
                "conversation_id": "conv-activity",
                "turn_index": 0,
                "turn_sha256": "a" * 64,
            },
        },
        "request_ref": {
            "artifact_id": str(uuid4()),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "media_type": "application/json",
            "size_bytes": len(payload),
            "availability": "unavailable",
        },
        "contract_sha256": "b" * 64,
        "repository_id": "repo-1",
        "skill": "inspect",
        "execution_epoch": execution_epoch,
        "deadline_utc": "2030-01-01T00:00:00Z",
    }


def _admitted_service(tmp_path, *, execution_epoch: int = 0):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    receipt = service.submit_task(
        request_id="request-1",
        payload_sha256="c" * 64,
        admission_payload=_admission_payload(execution_epoch=execution_epoch),
    )
    receipt.wait(5)
    assert receipt.task_id is not None
    return service, receipt.task_id


def test_tool_start_and_finish_commit_reviewed_durable_events(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("run_test")
        activity.finish_tool(
            call_id=opened.call_id,
            tool_name="run_test",
            execution="ok",
            domain="fail",
            reason=None,
            exit_code=1,
            duration_ms=17,
            evidence_ids=("run_test:0",),
        )

        events = service.replay()
        assert [event["kind"] for event in events] == [
            "task.admitted",
            "tool.started",
            "tool.finished",
        ]
        started = events[1]["payload"]
        finished = events[2]["payload"]
        assert started["call_id"] == finished["call_id"] == opened.call_id
        assert started["execution_epoch"] == finished["execution_epoch"] == 0
        assert started["deadline_utc"] == "2030-01-01T00:00:00Z"
        assert started["arguments_ref"] is None
        assert finished["execution"] == "ok"
        assert finished["domain"] == "fail"
        assert finished["reason_code"] == "completed"
        assert finished["evidence_ids"] == ["run_test:0"]
        assert activity.open_call is None
    finally:
        service.close()


def test_stale_execution_epoch_fails_before_a_tool_start_is_committed(tmp_path):
    service, task_id = _admitted_service(tmp_path, execution_epoch=0)
    try:
        activity = DurableToolActivity(
            service,
            task_id=task_id,
            execution_epoch=1,
            deadline_utc="2030-01-01T00:00:00Z",
        )
        with pytest.raises(TaskStateConflict, match="execution_epoch"):
            activity.start_tool("repo_info")
        assert [event["kind"] for event in service.replay()] == ["task.admitted"]
        assert activity.open_call is None
    finally:
        service.close()


def test_finish_requires_the_exact_open_call(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("repo_info")
        with pytest.raises(DurableActivityError, match="does not match"):
            activity.finish_tool(
                call_id=str(uuid4()),
                tool_name="repo_info",
                execution="ok",
                domain="pass",
                reason=None,
                exit_code=0,
                duration_ms=1,
            )
        assert activity.open_call == opened
        assert [event["kind"] for event in service.replay()] == [
            "task.admitted",
            "tool.started",
        ]
    finally:
        service.close()


def test_new_tool_reason_cannot_be_silently_projected_into_durable_history():
    with pytest.raises(DurableActivityError, match="unmapped"):
        durable_tool_reason(execution="error", reason="brand_new_reason")


def test_known_tool_reason_projection_is_explicit():
    assert durable_tool_reason(execution="blocked", reason="policy_denied") == "policy_denied"
    assert durable_tool_reason(execution="error", reason="orchestrator_timeout") == "tool_timeout"
    assert durable_tool_reason(execution="error", reason="stale_binary") == "stale_evidence"



def test_failed_call_retains_typed_reason_and_bounded_detail_as_result(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("read_file")
        activity.finish_tool(
            call_id=opened.call_id,
            tool_name="read_file",
            execution="error",
            domain="unknown",
            reason="not_found",
            exit_code=None,
            duration_ms=2,
            failure_detail="'src/ring.cpp'   is\nnot a file" + "x" * 500,
        )
        finished = service.replay()[-1]["payload"]
        # The reviewed vocabulary still folds not_found into invalid_input ...
        assert finished["reason_code"] == "invalid_input"
        # ... and the typed reason and the tool's own message are retained beside it.
        ref = finished["result_ref"]
        assert ref["availability"] == "retained"
        assert ref["media_type"] == "application/json"
        retained = json.loads(service.store.artifact_bytes(ref))
        assert retained["schema"] == "lca.tool-failure/3"
        assert retained["process_started"] is None
        assert retained["tool_name"] == "read_file"
        assert retained["tool_reason"] == "not_found"
        assert retained["detail"].startswith("'src/ring.cpp' is not a file")
        assert len(retained["detail"]) == 300
    finally:
        service.close()


def test_clean_call_retains_no_result(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("run_test")
        activity.finish_tool(
            call_id=opened.call_id, tool_name="run_test", execution="ok", domain="fail",
            reason=None, exit_code=1, duration_ms=3,
        )
        assert service.replay()[-1]["payload"]["result_ref"] is None
    finally:
        service.close()


def test_failure_detail_on_a_clean_call_is_refused_before_anything_commits(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("run_test")
        with pytest.raises(DurableActivityError, match="no failure detail"):
            activity.finish_tool(
                call_id=opened.call_id, tool_name="run_test", execution="ok", domain="pass",
                reason=None, exit_code=0, duration_ms=1, failure_detail="looked fine",
            )
        assert activity.open_call == opened
        assert [event["kind"] for event in service.replay()][-1] == "tool.started"
    finally:
        service.close()


def test_a_result_reference_never_commits_without_its_exact_bytes(tmp_path):
    service, task_id = _admitted_service(tmp_path)
    try:
        activity = DurableToolActivity.from_task(service, task_id)
        opened = activity.start_tool("read_file")
        ref, payload = tool_failure_artifact(
            tool_name="read_file", tool_reason="bad_arguments", failure_detail="end before start",
        )
        finished = {
            "call_id": opened.call_id, "tool_name": "read_file", "execution": "error",
            "domain": "unknown", "reason_code": "invalid_input", "exit_code": None,
            "duration_ms": 1, "evidence_ids": [], "result_ref": ref, "execution_epoch": 0,
        }
        for bad in (None, payload + b" "):
            receipt = service.append("tool.finished", finished, task_id=task_id, result_bytes=bad)
            with pytest.raises(ArtifactIntegrityError):
                receipt.wait(5)
        assert [event["kind"] for event in service.replay()][-1] == "tool.started"
        orphan = service.append(
            "tool.finished",
            {**finished, "result_ref": None}, task_id=task_id, result_bytes=payload,
        )
        with pytest.raises(ArtifactIntegrityError, match="need a tool.finished result_ref"):
            orphan.wait(5)
    finally:
        service.close()


@pytest.mark.parametrize("followthrough", ["apply", "schema_then_apply", "stop"])
def test_proposal_survives_candidate_cleanup_and_explains_followthrough(sandbox, tmp_path, followthrough):
    sandbox.scenario("compile_error")
    before = (sandbox.root / "src/ring_buffer.cpp").read_bytes()
    service, task_id = _admitted_service(tmp_path)
    activity = DurableToolActivity.from_task(service, task_id)
    token = CancellationToken(task_id, 0)
    turns = [ChatResponse(tool_calls=[tool_call("propose_patch", {
        "path": "src/ring_buffer.cpp", "find": "++count;", "replace": "++count_;",
        "rationale": "repair undefined identifier",
    }, "p1")])]

    def latest_id(messages):
        results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        return next(r["data"]["patch_id"] for r in reversed(results) if "patch_id" in r.get("data", {}))

    if followthrough == "stop":
        def stop(messages):
            token.request()
            return ChatResponse(content="Cannot complete before Stop.")
        turns += [stop, ChatResponse(content="Stopped.")]
    else:
        if followthrough == "schema_then_apply":
            turns.append(ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": 17}, "bad")]))
        turns.append(lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": latest_id(m)}, "a1")]))
        turns.append(ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "failure", "summary": "not independently verified", "evidence_ids": [],
        }, "s")]))
    manager = GitWorkspaceManager(tmp_path / "workspaces", controller_commit="c" * 40)
    controller = TaskController(load_repo_config(sandbox.root), lambda: ScriptedClient(turns),
                                EventBuffer(service.stream_id), allow_execution=True, workspaces=manager)
    try:
        result = controller.run("fix the build", skill_name="fix-build-failure", task_id=task_id,
                                durable_activity=activity, cancellation_probe=token)
        finishes = [e["payload"] for e in service.replay() if e["kind"] == "tool.finished"]
        proposal = finishes[0]
        assert proposal["tool_name"] == "propose_patch" and proposal["domain"] == "pass"
        ref = proposal["result_ref"]
        retained = json.loads(service.store.artifact_bytes(ref))
        assert retained["schema"] == "lca.patch-proposal/1"
        assert retained["data"]["path"] == str(Path("src/ring_buffer.cpp"))
        assert retained["data"]["rationale"] == "repair undefined identifier"
        assert "++count_;" in retained["data"]["diff"]
        assert retained["claim"] == "proposal only; not applied or verified"
        applies = [f for f in finishes if f["tool_name"] == "apply_patch"]
        if followthrough == "stop":
            assert applies == []
            assert result.metrics["candidate"]["workspace_removed"] is True
        else:
            assert applies[-1]["execution"] == "ok"
            if followthrough == "schema_then_apply":
                assert applies[0]["execution"] == "error"
                failure = json.loads(service.store.artifact_bytes(applies[0]["result_ref"]))
                assert failure["tool_reason"] == "bad_arguments"
                assert "patch_id" in failure["detail"]
        assert result.verified_at_completion is False
        assert (sandbox.root / "src/ring_buffer.cpp").read_bytes() == before
        database = tmp_path / "session.db"
    finally:
        service.close()
    reopened = SQLiteSessionStore(database)
    assert json.loads(reopened.artifact_bytes(ref)) == retained
    assert (sandbox.root / "src/ring_buffer.cpp").read_bytes() == before


@pytest.mark.parametrize("refusal", ["policy_denied", "approval_declined"])
def test_pre_handler_apply_refusal_is_retained_without_editing(sandbox, tmp_path, refusal):
    service, task_id = _admitted_service(tmp_path)
    activity = DurableToolActivity.from_task(service, task_id)
    repo = load_repo_config(sandbox.root)
    repo = replace(repo, policy=replace(repo.policy, allow_patch=refusal != "policy_denied"))
    registry, _, _ = build_registry(repo)
    registry = wrap_registry_with_durable_activity(registry, activity)
    before = (sandbox.root / "src/ring_buffer.cpp").read_bytes()
    client = ScriptedClient([ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": "never-proposed"}, "a")]),
                             ChatResponse(content="refused")])
    try:
        worker = Orchestrator(repo=repo, registry=registry, client=client, skills=SkillLibrary([]),
                              approval=lambda *args: False,
                              observer=lambda k, p: TaskController._retain_rejected_tool(activity, p)
                              if k == "tool_rejected" else None)
        worker.run("apply", condition="control")
        finished = service.replay()[-1]["payload"]
        assert finished["tool_name"] == "apply_patch" and finished["execution"] == "blocked"
        failure = json.loads(service.store.artifact_bytes(finished["result_ref"]))
        assert failure["tool_reason"] == refusal
        assert (sandbox.root / "src/ring_buffer.cpp").read_bytes() == before
    finally:
        service.close()
