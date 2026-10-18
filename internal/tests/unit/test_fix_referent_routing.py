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
from local_agent.session.task_history import (
    DurableTaskHistory,
    RetainedTaskResult,
    TaskCandidate,
    TaskObservation,
)


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


def test_fix_routes_use_the_fixable_subset_without_hiding_diagnostics():
    old, current = str(uuid4()), str(uuid4())
    eligible = (old, current)
    kinds = {old: "test", current: "test"}

    implicit = decide_route(
        "fix it", active_repo_count=1, eligible_task_ids=eligible,
        fixable_task_ids=(current,), failure_kinds=kinds,
    )
    assert implicit.reference_ids == (current,)
    assert decide_route(
        f"fix task {old}", active_repo_count=1, eligible_task_ids=eligible,
        fixable_task_ids=(current,), failure_kinds=kinds,
    ).reason_code == "superseded_task_reference"
    explained = decide_route(
        f"why did task {old} fail?", active_repo_count=1,
        eligible_task_ids=eligible, fixable_task_ids=(current,), failure_kinds=kinds,
    )
    assert explained.reference_ids == (old,)


def test_fix_it_with_no_current_failure_does_not_route_old_history():
    old = str(uuid4())
    decision = decide_route(
        "fix it", active_repo_count=1, eligible_task_ids=(old,),
        fixable_task_ids=(), failure_kinds={old: "test"},
    )
    assert (decision.action, decision.reason_code) == (
        RouteAction.CLARIFY, "no_eligible_task_reference",
    )


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


def _admitted(seq, task_id, *, check="run-tests", repository="repo", conversation="conv"):
    rule = "run-tests/v1" if check == "run-tests" else "build-and-test/v1"
    return {
        "sequence": seq, "task_id": task_id, "kind": "task.admitted",
        "payload": {
            "origin": {
                "kind": "user_rule", "rule_id": rule,
                "turn_ref": {
                    "conversation_id": conversation, "turn_index": seq,
                    "turn_sha256": f"{seq:x}".rjust(64, "0"),
                },
            },
            "repository_id": repository, "skill": check,
        },
    }


def _verdict(seq, task_id, verdict):
    return {
        "sequence": seq, "task_id": task_id, "kind": "task.verdict",
        "payload": {"completion": {"verdict_block": {"verdict": verdict}}},
    }


def _plain_admitted(seq, task_id, *, conversation="conv"):
    return {
        "sequence": seq, "task_id": task_id, "kind": "task.admitted",
        "payload": {
            "origin": {
                "kind": "user_direct",
                "turn_ref": {
                    "conversation_id": conversation, "turn_index": seq,
                    "turn_sha256": f"{seq:x}".rjust(64, "0"),
                },
            },
            "repository_id": "repo", "skill": None,
        },
    }


def _result(task_id, verdict, *, ran=True):
    return RetainedTaskResult(
        task_id=task_id, status="failed" if verdict == "FAILED" else "completed",
        verdict=verdict, answer=verdict, evidence_ids=(), verification_ran=ran,
        verified_at_completion=verdict == "VERIFIED",
    )


class _ResultHistory(DurableTaskHistory):
    def __init__(self, events, results):
        super().__init__(_Store(events), stream_id="s")
        self.results = results

    def result_for_task(self, task_id):
        return self.results.get(task_id)


def test_durable_fixable_failures_follow_the_latest_executed_check_state():
    old, current = str(uuid4()), str(uuid4())
    events = [
        _admitted(1, old), _verdict(2, old, "FAILED"),
        _admitted(3, current), _verdict(4, current, "FAILED"),
    ]
    history = _ResultHistory(events, {
        old: _result(old, "FAILED"), current: _result(current, "FAILED"),
    })
    assert history.fixable_failure_task_ids("conv") == (current,)

    history.results[current] = _result(current, "VERIFIED")
    events[-1] = _verdict(4, current, "VERIFIED")
    assert history.fixable_failure_task_ids("conv") == ()


def test_noncheck_failure_stays_fixable_but_unexecuted_check_fails_closed():
    plain, check = str(uuid4()), str(uuid4())
    history = _ResultHistory([
        _plain_admitted(1, plain), _verdict(2, plain, "FAILED"),
        _admitted(3, check), _verdict(4, check, "FAILED"),
    ], {
        plain: _result(plain, "FAILED"), check: _result(check, "FAILED", ran=False),
    })
    assert history.fixable_failure_task_ids("conv") == (plain,)


@pytest.mark.parametrize(("later", "ran", "check", "repository"), [
    ("REFUSED", False, "run-tests", "repo"),
    ("NO_VERDICT", False, "run-tests", "repo"),
    ("VERIFIED", True, "run-build", "repo"),
    ("VERIFIED", True, "run-tests", "other"),
])
def test_only_a_later_executed_same_repo_check_supersedes_fix_authority(
    later, ran, check, repository,
):
    old, other = str(uuid4()), str(uuid4())
    history = _ResultHistory([
        _admitted(1, old), _verdict(2, old, "FAILED"),
        _admitted(3, other, check=check, repository=repository),
        _verdict(4, other, later),
    ], {
        old: _result(old, "FAILED"), other: _result(other, later, ran=ran),
    })
    assert history.fixable_failure_task_ids("conv") == (old,)


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
    def __init__(self, candidates, observations, kinds, *, fixable=None):
        self._candidates, self._observations, self._kinds = tuple(candidates), dict(observations), kinds
        self._fixable = tuple(candidates) if fixable is None else tuple(fixable)

    def latest(self, conversation_id):
        return None

    def failure_candidates(self, conversation_id):
        return self._candidates

    def observation_for(self, candidate):
        return self._observations.get(candidate.task_id)

    def failure_kind(self, task_id):
        return self._kinds.get(task_id)

    def fixable_failure_task_ids(self, conversation_id):
        return tuple(candidate.task_id if isinstance(candidate, TaskCandidate) else candidate
                     for candidate in self._fixable)


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


def test_gateway_fixes_only_current_check_but_still_explains_replaced_history():
    runner = _Runner()
    gateway = ConversationGateway(
        _Chat(), SimpleNamespace(repo=object(), run=None), EventBuffer("s"), task_runner=runner,
    )
    old_index = len(gateway.session.turns)
    append_turn(gateway.session, "user", "run the tests", gateway.runtime_index)
    old_ref = turn_ref(gateway.session, old_index)
    current_index = len(gateway.session.turns)
    append_turn(gateway.session, "user", "run the tests", gateway.runtime_index)
    current_ref = turn_ref(gateway.session, current_index)
    old, current = str(uuid4()), str(uuid4())
    observations = {
        old: TaskObservation(
            old, old_ref, "failed", "FAILED", "old test failure", (),
            configured_check="run-tests",
        ),
        current: TaskObservation(
            current, current_ref, "failed", "FAILED", "current test failure", (),
            configured_check="run-tests",
        ),
    }
    gateway.task_history = _History(
        (TaskCandidate(old, old_ref, "run-tests"),
         TaskCandidate(current, current_ref, "run-tests")),
        observations,
        {old: "test", current: "test"},
        fixable=(current,),
    )

    gateway.turn("fix it")
    assert len(runner.calls) == 1
    assert current in runner.calls[0][0] and old not in runner.calls[0][0]

    answer = gateway.turn(f"why did task {old} fail?")
    assert len(runner.calls) == 1
    assert old in answer and "old test failure" in answer
