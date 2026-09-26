"""The code-changing journey through the same object graph as the public Session Hub.

Gateway -> durable route/admission -> cancellable executor -> admitted controller ->
TaskController -> candidate worktree -> retained lca.task-result/2 -> durable history.
Only the models are scripted. Build, test, git and the durable store are real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.protocol import ChatResponse
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.contracts import TaskOutcome
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager

RING = "src/ring_buffer.cpp"


class NoConversationModel:
    """Every turn in this journey is deterministic; the conversation model is never used."""

    def chat(self, messages, tools=None):
        raise AssertionError("deterministic journey reached the conversation model")


def _patch_id(messages):
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                pid = (json.loads(m.get("content") or "{}").get("data") or {}).get("patch_id")
            except json.JSONDecodeError:
                continue
            if pid:
                return pid
    raise AssertionError("no patch_id")


def _build_turns():
    return [
        ChatResponse(tool_calls=[tool_call("build_target", {}, "b1")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "failure", "summary": "compile errors in ring_buffer.cpp",
            "evidence_ids": ["build_target:0"]}, "b2")]),
        ChatResponse(content="The build fails: two compile errors in src/ring_buffer.cpp."),
    ]


def _fix_turns():
    return [
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": RING, "find": "++count;", "replace": "++count_;"}, "f1")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "f2")]),
        ChatResponse(tool_calls=[tool_call("propose_patch", {"path": RING, "find": "return count_ == 0 }", "replace": "return count_ == 0; }"}, "f3")]),
        lambda m: ChatResponse(tool_calls=[tool_call("apply_patch", {"patch_id": _patch_id(m)}, "f4")]),
        ChatResponse(tool_calls=[tool_call("build_target", {}, "f5")]),
        ChatResponse(tool_calls=[tool_call("submit_answer", {
            "claim": "success", "summary": "fixed", "evidence_ids": ["build_target:4"]}, "f6")]),
        ChatResponse(content="Fixed both compile errors."),
    ]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def test_build_fails_then_fix_it_apply_and_commit_through_the_public_path(sandbox, tmp_path):
    sandbox.scenario("compile_error")
    config = sandbox.root / ".local-agent.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("allow_commit = false", "allow_commit = true"),
                      encoding="utf-8")
    _git(sandbox.root, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qam", "broken, commits allowed")
    _git(sandbox.root, "config", "user.email", "dev@example.invalid")
    _git(sandbox.root, "config", "user.name", "Dev")
    broken = (sandbox.root / RING).read_bytes()

    scripts = iter([_build_turns(), _fix_turns()])
    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                    stream_id=str(uuid4()), session_id=str(uuid4()))
    try:
        events = EventBuffer(service.stream_id)
        controller = TaskController(
            load_repo_config(sandbox.root), lambda: ScriptedClient(list(next(scripts))), events,
            allow_execution=True,
            workspaces=GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40),
        )
        executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
        history = DurableTaskHistory(service.store, stream_id=service.stream_id)
        gateway = ConversationGateway(
            NoConversationModel(), controller, events,
            task_runner=DurableTaskAdmissionRunner(executor),
            task_history=history,
            route_events=DurableRouteEvents(service),
        )

        # 1. The build fails, and that failure is durable.
        gateway.turn("build it")
        failed = gateway.last_result
        assert failed.outcome is TaskOutcome.FAIL, failed.answer
        # A build observed failing is FAILED/verification_failed, never "cleanup unknown".
        assert failed.reason_code == "verification_failed"
        assert failed.metrics["proof_binding"]["scope"] == "observed_build_failure"
        assert history.failure_kind(failed.task_id) == "build"

        # 2. "fix it" resolves that one failed build and prepares a proven candidate.
        gateway.turn("fix it")
        prepared = gateway.last_result
        assert prepared.outcome is TaskOutcome.PASS, prepared.answer
        assert prepared.verified_at_completion is True
        assert (sandbox.root / RING).read_bytes() == broken
        retained = history.result_for_task(prepared.task_id)
        assert retained.candidate.role == "prepared" and retained.candidate.retained is True
        assert retained.candidate.paths == (RING,)

        # 3. The user applies exactly that candidate, admitted as a controller action.
        gateway.turn(f"/apply {prepared.task_id}")
        applied = gateway.last_result
        assert applied.outcome is TaskOutcome.PASS, applied.answer
        assert "++count_;" in (sandbox.root / RING).read_text(encoding="utf-8")
        applied_facts = history.result_for_task(applied.task_id).candidate
        assert (applied_facts.role, applied_facts.candidate_task_id) == ("applied", prepared.task_id)

        # 4. And commits it: only that file, on the current branch, nothing pushed.
        head_before = _git(sandbox.root, "rev-parse", "HEAD").strip()
        gateway.turn(f"/commit {prepared.task_id}")
        committed = gateway.last_result
        assert committed.outcome is TaskOutcome.PASS, committed.answer
        commit = history.result_for_task(committed.task_id).candidate.commit
        assert _git(sandbox.root, "rev-parse", f"{commit}^").strip() == head_before
        assert _git(sandbox.root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).split() == [RING]
        assert _git(sandbox.root, "status", "--porcelain") == ""

        # Every step was admitted durably and closed; nothing is left running.
        for task_id in (failed.task_id, prepared.task_id, applied.task_id, committed.task_id):
            record = service.store.task_record(task_id)
            assert record is not None and bool(record["terminal"]), task_id
    finally:
        service.close()
