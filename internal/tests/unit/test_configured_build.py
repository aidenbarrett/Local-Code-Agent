"""Declared build commands are mechanical facts, not a model's recollection."""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from local_agent.config import load_repo_config
from local_agent.session.configured_build import (
    CONFIGURED_BUILD, ConfiguredBuildPlan, configured_build_sha256, explain_configured_build,
)
from local_agent.session.intents import RULE_CONFIGURED_BUILD, RouteAction, decide_route


def _data():
    return {"name": "example", "default_profile": "debug", "profiles": {
        "debug": {"configure": ["cmake", "-S", ".", "-B", "build dir"],
                  "build": ["cmake", "--build", "build dir"], "test": ["ctest", "--test-dir", "build dir"]},
        "release": {"configure": [], "build": ["ninja", "-C", "release"], "test": []},
    }}


@pytest.mark.parametrize("question", ["how is this repository built and tested?",
                                     "How is the repo built?", "how is this project tested!"])
def test_exact_configuration_questions_select_the_model_free_plan(question):
    route = decide_route(question, active_repo_count=1)
    assert (route.rule_id, route.skill) == (RULE_CONFIGURED_BUILD, CONFIGURED_BUILD)
    assert decide_route(question, active_repo_count=0).action is RouteAction.CLARIFY


@pytest.mark.parametrize("question", ["explain this repository", "where is build_queue defined?",
                                     "how is this repository built and tested? then fix it"])
def test_semantic_and_composite_requests_keep_the_worker(question):
    assert decide_route(question, active_repo_count=1).skill != CONFIGURED_BUILD


def test_rendering_keeps_each_profiles_exact_argv_and_claims_no_execution():
    text = explain_configured_build(_data())
    for profile in _data()["profiles"].values():
        for argv in profile.values():
            assert not argv or json.dumps(argv) in text
    assert "test: not configured" in text
    assert "no build or test was run" in text
    assert configured_build_sha256("a" * 64) != configured_build_sha256("b" * 64)


@pytest.mark.parametrize("data", [None, {}, {"name": "x", "default_profile": "bad", "profiles": {}},
                                {"name": "x", "default_profile": "debug", "profiles": {"debug": {}}}])
def test_missing_or_truncated_configuration_stays_unknown(data):
    plan = ConfiguredBuildPlan()
    (first,) = plan.chat([]).tool_calls
    assert first.name == "repo_info"
    (answer,) = plan.chat([{"role": "tool", "name": "repo_info", "content": json.dumps({
        "execution": "ok", "result": "pass", "data": data})}]).tool_calls
    assert answer.arguments["claim"] == "diagnosis"
    assert "unknown" in answer.arguments["summary"]


@pytest.mark.parametrize("result", ["fail", "unknown", None])
def test_non_passing_tool_facts_never_supply_declared_commands(result):
    (answer,) = ConfiguredBuildPlan().chat([{
        "role": "tool", "name": "repo_info", "content": json.dumps({
            "execution": "ok", "result": result, "data": _data(),
        }),
    }]).tool_calls
    assert "unknown" in answer.arguments["summary"]
    assert "cmake" not in answer.arguments["summary"]


def test_actual_tool_result_envelope_supplies_only_configured_facts():
    from local_agent.tools.tool_primitives import ToolResult

    result = ToolResult(ok=True, summary="configuration", data=_data())
    (answer,) = ConfiguredBuildPlan().chat([{
        "role": "tool", "name": "repo_info", "content": result.to_json(),
    }]).tool_calls
    assert "default profile: debug" in answer.arguments["summary"]
    assert answer.arguments["claim"] == "diagnosis"


def test_durable_gateway_reports_configuration_without_a_model_or_build(sandbox, tmp_path):
    from local_agent.session.cancellable_task_executor import CancellableDurableTaskExecutor
    from local_agent.session.conversation_gateway import ConversationGateway
    from local_agent.session.durable_routes import DurableRouteEvents
    from local_agent.session.durable_task_controller import AdmittedDurableTaskController
    from local_agent.session.event_buffer import EventBuffer
    from local_agent.session.session_event_service import DurableSessionService
    from local_agent.session.session_store import SQLiteSessionStore
    from local_agent.session.task_admission import DurableTaskAdmissionRunner
    from local_agent.session.task_controller import TaskController
    from local_agent.session.task_history import DurableTaskHistory

    def no_model(*args, **kwargs):
        raise AssertionError("the configuration question reached a model")

    service = DurableSessionService(SQLiteSessionStore(tmp_path / "session.db"),
                                   stream_id=str(uuid4()), session_id=str(uuid4()))
    events = EventBuffer(service.stream_id)
    repo = load_repo_config(sandbox.root)
    controller = TaskController(repo, no_model, events, allow_execution=False)
    executor = CancellableDurableTaskExecutor(service, AdmittedDurableTaskController(service, controller))
    gateway = ConversationGateway(
        type("NoConversationModel", (), {"chat": no_model})(), controller, events,
        task_runner=DurableTaskAdmissionRunner(executor),
        task_history=DurableTaskHistory(service.store, stream_id=service.stream_id),
        route_events=DurableRouteEvents(service),
    )
    try:
        answer = gateway.turn("how is this repository built and tested?")
        for argv in (repo.profile().configure, repo.profile().build, repo.profile().test):
            assert json.dumps(argv) in answer
        assert "no build or test was run" in answer
        assert gateway.last_result.verified_at_completion is False
        assert gateway.last_result.metrics["llm_calls"] == 0
        finished = [e["payload"]["tool_name"] for e in service.store.replay(service.stream_id, limit=1000)
                    if e["kind"] == "tool.finished"]
        assert finished == ["repo_info"]
        assert not repo.build_path.exists()
    finally:
        service.close()
