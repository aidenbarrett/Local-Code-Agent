"""`fix it` resolves one durable failed task and picks the fix from typed tool facts."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import append_turn, turn_ref
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_FIX_REFERENT, RouteAction, decide_route
from local_agent.session.task_history import DurableTaskHistory, TaskCandidate, TaskObservation


@pytest.mark.parametrize("text", ["fix it", "Fix it.", "fix that", "FIX THAT!"])
def test_fix_it_with_one_failed_build_routes_to_the_build_fix(text):
    task_id = str(uuid4())
    decision = decide_route(text, active_repo_count=1, eligible_task_ids=(task_id,),
                            failure_kinds={task_id: "build"})
    assert decision.action is RouteAction.WORK
    assert decision.source is RouteSource.RULE
    assert (decision.rule_id, decision.skill) == (RULE_FIX_REFERENT, "fix-build-failure")
    assert decision.reference_ids == (task_id,)


def test_fix_it_with_one_failed_test_run_routes_to_the_test_fix():
    task_id = str(uuid4())
    decision = decide_route("fix it", active_repo_count=1, eligible_task_ids=(task_id,),
                            failure_kinds={task_id: "test"})
    assert decision.skill == "fix-test-failure"


def test_fix_it_never_guesses_between_failed_tasks_or_unknown_kinds():
    one, two = str(uuid4()), str(uuid4())
    kinds = {one: "build", two: "test"}
    assert decide_route("fix it", active_repo_count=1, eligible_task_ids=(one, two),
                        failure_kinds=kinds).reason_code == "ambiguous_task_reference"
    assert decide_route("fix it", active_repo_count=1, eligible_task_ids=(),
                        failure_kinds={}).reason_code == "no_eligible_task_reference"
    unknown = decide_route("fix it", active_repo_count=1, eligible_task_ids=(one,), failure_kinds={})
    assert (unknown.action, unknown.reason_code) == (RouteAction.CLARIFY, "unfixable_failure_kind")


def test_explicit_fix_task_selects_one_of_many():
    one, two = str(uuid4()), str(uuid4())
    decision = decide_route(f"fix task {two}", active_repo_count=1, eligible_task_ids=(one, two),
                            failure_kinds={one: "build", two: "test"})
    assert (decision.skill, decision.reference_ids) == ("fix-test-failure", (two,))
    assert decide_route(f"fix task {two[:8]}", active_repo_count=1, eligible_task_ids=(one, two),
                        failure_kinds={}).reason_code == "invalid_task_reference"
    assert decide_route(f"fix task {uuid4()}", active_repo_count=1, eligible_task_ids=(one,),
                        failure_kinds={one: "build"}).reason_code == "ineligible_task_reference"


def test_fix_it_needs_an_active_repository():
    task_id = str(uuid4())
    decision = decide_route("fix it", active_repo_count=0, eligible_task_ids=(task_id,),
                            failure_kinds={task_id: "build"})
    assert decision.action is RouteAction.CLARIFY


# ------------------------------------------------ typed failure kind from durable events


class _Store:
    def __init__(self, events):
        self.events = events

    def replay(self, stream_id, *, after, limit):
        return [e for e in self.events if e["sequence"] > after][:limit]


def _finished(seq, task_id, tool, domain, execution="ok"):
    return {"sequence": seq, "task_id": task_id, "kind": "tool.finished",
            "payload": {"tool_name": tool, "domain": domain, "execution": execution}}


def test_failure_kind_is_the_last_failing_build_or_test_tool_of_that_task():
    task_id, other = str(uuid4()), str(uuid4())
    history = DurableTaskHistory(_Store([
        _finished(1, task_id, "build_target", "fail"),
        _finished(2, task_id, "build_target", "pass"),
        _finished(3, task_id, "run_test", "fail"),
        _finished(4, task_id, "read_file", "fail"),
        _finished(5, other, "build_target", "fail"),
    ]), stream_id="s")
    assert history.failure_kind(task_id) == "test"
    assert history.failure_kind(other) == "build"


def test_orchestrator_errors_and_non_check_tools_are_not_a_fixable_kind():
    task_id = str(uuid4())
    history = DurableTaskHistory(_Store([
        _finished(1, task_id, "build_target", "unknown", execution="error"),
        _finished(2, task_id, "search_text", "fail"),
    ]), stream_id="s")
    assert history.failure_kind(task_id) is None


# ----------------------------------------------------------------- gateway, end to end


class _History:
    def __init__(self, candidates, observations, kinds):
        self._candidates, self._observations, self._kinds = tuple(candidates), dict(observations), kinds

    def latest(self, conversation_id):
        return None

    def failure_candidates(self, conversation_id):
        return self._candidates

    def observation_for(self, candidate):
        return self._observations.get(candidate.task_id)

    def failure_kind(self, task_id):
        return self._kinds.get(task_id)


class _Chat:
    def chat(self, messages, tools=None):
        raise AssertionError("a resolved fix referent must not reach the conversation model")


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return TaskResult(str(uuid4()), TaskOutcome.PASS, "candidate prepared", True)


def test_gateway_routes_fix_it_to_the_fix_for_the_one_failed_build():
    runner = _Runner()
    gateway = ConversationGateway(
        _Chat(), SimpleNamespace(repo=object(), run=None), EventBuffer("s"), task_runner=runner,
    )
    index = len(gateway.session.turns)
    append_turn(gateway.session, "user", "build it", gateway.runtime_index)
    ref = turn_ref(gateway.session, index)
    failed = str(uuid4())
    observation = TaskObservation(failed, ref, "failed", "FAILED", "ring_buffer.cpp:13 error", ("build_target:0",))
    gateway.task_history = _History([TaskCandidate(failed, ref)], {failed: observation}, {failed: "build"})

    gateway.turn("fix it")

    assert len(runner.calls) == 1
    task, kwargs = runner.calls[0]
    assert kwargs["skill"] == "fix-build-failure"
    assert kwargs["rule_id"] == RULE_FIX_REFERENT
    assert failed in task and "ring_buffer.cpp:13 error" in task
