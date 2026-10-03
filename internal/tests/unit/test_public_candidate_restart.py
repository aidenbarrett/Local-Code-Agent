"""Controller death through public candidate routes, with real Git and durable state."""
from __future__ import annotations

import json

import pytest

from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient
from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import conversation, create_session, new_session
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.durable_task_controller import AdmittedDurableTaskController
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.task_admission import DurableTaskAdmissionRunner
from local_agent.session.task_controller import TaskController
from local_agent.session.task_history import DurableTaskHistory
from local_agent.session.workspaces import GitWorkspaceManager, WorkspaceError
from test_public_path_code_journey import NoConversationModel, RING, _fix_turns, _git
from test_session_hub_product_path import _load_hub


def _gateway(root, service, manager, opened, turns=()):
    events = EventBuffer(service.stream_id)
    controller = TaskController(
        load_repo_config(root), lambda: ScriptedClient(list(turns)), events,
        allow_execution=True, workspaces=manager,
    )
    executor = CancellableDurableTaskExecutor(
        service, AdmittedDurableTaskController(service, controller),
    )
    return ConversationGateway(
        NoConversationModel(), controller, events, conversation=opened,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )


def _state(root):
    return tuple(_git(root, *args) for args in (
        ("rev-parse", "HEAD"), ("diff", "--cached", "--binary"),
        ("diff", "--binary"), ("status", "--porcelain=v1", "--untracked-files=all"),
    ))


@pytest.mark.parametrize("effect", ["apply", "commit"])
def test_public_candidate_effect_death_recovers_unknown_and_refuses_reissue(
    sandbox, tmp_path, monkeypatch, effect,
):
    hub = _load_hub()
    sandbox.scenario("compile_error")
    config = sandbox.root / ".local-agent.toml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "allow_commit = false", "allow_commit = true",
    ), encoding="utf-8")
    _git(sandbox.root, "config", "user.email", "restart@example.invalid")
    _git(sandbox.root, "config", "user.name", "Restart")
    _git(sandbox.root, "commit", "-qam", "broken, commits allowed")
    runtime = tmp_path / "runtime"
    session = new_session("fixture", "scripted", "CPU")
    create_session(runtime, session)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
    service, recovered = hub._open_durable_service(runtime, session.conversation_id)
    assert recovered == []
    candidate_id = None
    try:
        with conversation(runtime, session.conversation_id) as opened:
            gateway = _gateway(sandbox.root, service, manager, opened, _fix_turns())
            gateway.turn("fix the build")
            prepared = gateway.last_result
            assert prepared.outcome.value == "pass", prepared.answer
            candidate_id = prepared.task_id
            if effect == "commit":
                gateway.turn(f"/apply {candidate_id}")
                assert gateway.last_result.outcome.value == "pass"

            before = _state(sandbox.root)
            # Unrelated staged and untracked work must survive death and recovery.
            (sandbox.root / "notes.txt").write_text("keep staged\n", encoding="utf-8")
            _git(sandbox.root, "add", "notes.txt")
            (sandbox.root / "scratch.txt").write_text("keep untracked\n", encoding="utf-8")
            index_before = _git(sandbox.root, "diff", "--cached", "--binary")
            calls = []
            with monkeypatch.context() as death:
                if effect == "apply":
                    def die_before_receipt(*args, **kwargs):
                        calls.append("apply")
                        assert "++count_;" in (sandbox.root / RING).read_text(encoding="utf-8")
                        raise SystemExit("death after apply before receipt")
                    death.setattr(manager, "record_applied", die_before_receipt)
                else:
                    real_git = manager._git

                    def die_after_git(root, *args, **kwargs):
                        result = real_git(root, *args, **kwargs)
                        if args and args[0] == "commit":
                            assert result.returncode == 0
                            calls.append("commit")
                            raise SystemExit("death after commit before receipt")
                        return result
                    death.setattr(manager, "_git", die_after_git)
                with pytest.raises(SystemExit, match="death after"):
                    gateway.turn(f"/{effect} {candidate_id}")
            assert calls == [effect]
            receipt_path = manager.workspaces_root / f"{candidate_id}.applied.json"
            if effect == "apply":
                assert not receipt_path.exists()
            else:
                assert "committed" not in json.loads(receipt_path.read_text(encoding="utf-8"))
            unfinished = [event["task_id"] for event in service.replay()
                          if event["kind"] == "task.admitted"
                          and not service.store.task_record(event["task_id"])["terminal"]]
            assert len(unfinished) == 1
            interrupted_id = unfinished[0]
            assert _git(sandbox.root, "diff", "--cached", "--binary") == index_before
            at_death = _state(sandbox.root)
            if effect == "commit":
                assert at_death[0] != before[0]
                assert _git(sandbox.root, "rev-parse", "HEAD^") == before[0]
            else:
                assert at_death[0] == before[0]
            bytes_at_death = (sandbox.root / RING).read_bytes()
    finally:
        service.close()

    # Use the public Hub's actual startup recovery, fresh manager and saved conversation.
    service, recovered = hub._open_durable_service(runtime, session.conversation_id)
    manager = GitWorkspaceManager(tmp_path / "ws", controller_commit="c" * 40)
    try:
        manager.reap_orphans()
        assert recovered == [interrupted_id]
        assert service.recover_unknown_tasks() == []
        assert _state(sandbox.root) == at_death
        result = DurableTaskHistory(service.store, stream_id=service.stream_id).result_for_task(
            interrupted_id,
        )
        assert result is not None
        assert result.status == "unknown"
        assert result.verdict == "NO_VERDICT"
        record = service.store.task_record(interrupted_id)
        retained = json.loads(service.store.artifact_bytes(record["result_ref"]))
        assert retained["verified_at_completion"] is False
        completion = next(event["payload"]["completion"] for event in service.replay()
                          if event["kind"] == "task.verdict" and event["task_id"] == interrupted_id)
        assert completion["status"] == "unknown"
        assert completion["verdict_block"]["reason_code"] == "controller_restarted"
        with conversation(runtime, session.conversation_id) as opened:
            gateway = _gateway(sandbox.root, service, manager, opened)
            answer = gateway.turn(f"/{effect} {candidate_id}")
            assert gateway.last_result.outcome.value in {"fail", "blocked"}, answer
            # The refusal names why, and the why is the interrupted effect itself.
            if effect == "apply":
                assert gateway.last_result.reason_code == "scope_changed", answer
                assert "No change was applied" in answer and RING in answer, answer
                assert "already holds exactly this change" in answer, answer
            else:
                assert gateway.last_result.reason_code == "invalid_input", answer
                assert "already what HEAD contains" in answer, answer
            assert _state(sandbox.root) == at_death
            assert (sandbox.root / RING).read_bytes() == bytes_at_death
            assert (sandbox.root / "scratch.txt").read_text(encoding="utf-8") == "keep untracked\n"
    finally:
        service.close()
        if candidate_id is not None:
            try:
                workspace, _ = manager.load(candidate_id)
            except WorkspaceError:
                pass  # A successful apply may already have discarded its candidate.
            else:
                manager.discard(workspace)
