from __future__ import annotations
from types import SimpleNamespace
from uuid import uuid4
import pytest
from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.durable_routes import DurableRouteEvents
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_BUILD_AND_TEST
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore

class NoConversationModel:
    def __init__(self): self.calls = 0
    def chat(self, messages, tools=None):
        self.calls += 1
        raise AssertionError("Build it must not call the conversation model")

class ResultRunner:
    def __init__(self, result):
        self.result, self.calls = result, []
    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return self.result

def _service(tmp_path):
    return DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()), session_id=str(uuid4()))

def _gateway(service, runner, chat):
    controller = SimpleNamespace(repo=object())
    return ConversationGateway(chat, controller, EventBuffer(service.stream_id),
        task_runner=runner, route_events=DurableRouteEvents(service))

def _binding(scope, evidence_ids=()):
    return {"request_sha256": "a"*64, "scope": scope, "tree_sha256": "b"*64,
            "evidence_ids": list(evidence_ids)}

def _run(tmp_path, result):
    service, chat = _service(tmp_path), NoConversationModel()
    runner = ResultRunner(result)
    try:
        rendered = _gateway(service, runner, chat).turn("Build it.")
        assert chat.calls == 0
        task, kwargs = runner.calls[0]
        assert "User request:\nBuild it." in task
        assert f"rule_id={RULE_BUILD_AND_TEST}" in task
        assert kwargs["route_source"] == RouteSource.RULE
        assert kwargs["skill"] == "build-and-test"
        return rendered
    finally:
        service.close()

def test_build_pass_exposes_full_current_tree_proof(tmp_path):
    result = TaskResult("pass", TaskOutcome.PASS, "Build passed.", True,
        ("build_target:0",), {"proof_binding": _binding("full_build", ("build_target:0",))},
        verification_ran=True, reason_code="verification_passed")
    rendered = _run(tmp_path, result)
    assert "Controller: pass" in rendered
    assert "Proof scope: full_build" in rendered
    assert "Tree SHA-256: " + "b"*64 in rendered

def test_build_failure_exposes_observed_failure_proof(tmp_path):
    result = TaskResult("fail", TaskOutcome.FAIL, "Build failed.", False,
        ("build_target:0",), {"proof_binding": _binding("observed_build_failure", ("build_target:0",))},
        verification_ran=True, reason_code="verification_failed")
    rendered = _run(tmp_path, result)
    assert "Controller: fail" in rendered
    assert "Proof scope: observed_build_failure" in rendered

def test_targeted_build_remains_no_verdict(tmp_path):
    result = TaskResult("partial", TaskOutcome.NO_VERDICT, "Target passed only.", False,
        ("build_target:0",), {"proof_binding": _binding("targeted_build", ("build_target:0",))},
        verification_ran=True, reason_code="missing_evidence")
    rendered = _run(tmp_path, result)
    assert "Controller: no_verdict" in rendered
    assert "Proof scope: targeted_build" in rendered
    assert "Controller: pass" not in rendered

def test_unavailable_build_invents_no_proof(tmp_path):
    result = TaskResult("blocked", TaskOutcome.BLOCKED, "Build unavailable.", False,
        reason_code="unavailable_capability")
    rendered = _run(tmp_path, result)
    assert "Controller: blocked" in rendered
    assert "Proof scope:" not in rendered
    assert "Tree SHA-256:" not in rendered

def test_success_shaped_targeted_proof_fails_closed(tmp_path):
    result = TaskResult("invalid", TaskOutcome.PASS, "Target passed.", True,
        ("build_target:0",), {"proof_binding": _binding("targeted_build", ("build_target:0",))},
        verification_ran=True, reason_code="verification_passed")
    with pytest.raises(ValueError, match="full current-tree proof scope"):
        _run(tmp_path, result)
