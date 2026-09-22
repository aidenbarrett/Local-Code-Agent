from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from local_agent.session.contracts import RouteSource, TaskOutcome, TaskResult
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.conversation_store import append_turn, turn_ref
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.intents import RULE_TASK_DIAGNOSTIC, RouteAction, decide_route
from local_agent.session.task_history import TaskCandidate, TaskObservation


class ExplodingChat:
    calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        raise AssertionError("explicit durable referent must not reach the conversation model")


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, task, **kwargs):
        self.calls.append((task, kwargs))
        return TaskResult("task-result", TaskOutcome.FAIL, "diagnostic failed", False)


class History:
    def __init__(self, candidates, observations):
        self._candidates = tuple(candidates)
        self._observations = dict(observations)

    def latest(self, conversation_id):
        return None

    def failure_candidates(self, conversation_id):
        return self._candidates

    def observation_for(self, candidate):
        return self._observations.get(candidate.task_id)


def _gateway():
    chat = ExplodingChat()
    runner = Runner()
    gateway = ConversationGateway(
        chat,
        SimpleNamespace(
            repo=object(),
            run=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
        ),
        EventBuffer("s"),
        task_runner=runner,
    )
    return gateway, chat, runner


def _prior_turn(gateway, text: str):
    index = len(gateway.session.turns)
    append_turn(gateway.session, "user", text, gateway.runtime_index)
    return turn_ref(gateway.session, index)


def _observation(task_id: str, ref: dict[str, object]) -> TaskObservation:
    return TaskObservation(
        task_id=task_id,
        turn_ref=ref,
        status="failed",
        verdict="FAILED",
        answer=f"historical result for {task_id}",
        evidence_ids=(f"evidence:{task_id}",),
    )


def test_explicit_diagnostic_selects_one_candidate_without_guessing_across_many():
    one = str(uuid4())
    two = str(uuid4())
    decision = decide_route(
        f"Why did task {two} fail?",
        eligible_task_ids=(one, two),
    )
    assert decision.action == RouteAction.WORK
    assert decision.source == RouteSource.RULE
    assert decision.rule_id == RULE_TASK_DIAGNOSTIC
    assert decision.skill == "task-diagnostic"
    assert decision.reference_ids == (two,)


def test_explicit_diagnostic_canonicalises_uuid_case_but_not_uuid_shape():
    task_id = str(uuid4())
    upper = task_id.upper()
    accepted = decide_route(
        f"Why did task {upper} fail?",
        eligible_task_ids=(task_id,),
    )
    assert accepted.action == RouteAction.WORK
    assert accepted.reference_ids == (task_id,)

    compact = task_id.replace("-", "")
    rejected = decide_route(
        f"Why did task {compact} fail?",
        eligible_task_ids=(task_id,),
    )
    assert rejected.action == RouteAction.CLARIFY
    assert rejected.reason_code == "invalid_task_reference"


def test_explicit_diagnostic_refuses_uuid_outside_eligible_failed_set():
    eligible = str(uuid4())
    foreign = str(uuid4())
    decision = decide_route(
        f"Why did task {foreign} fail?",
        eligible_task_ids=(eligible,),
    )
    assert decision.action == RouteAction.CLARIFY
    assert decision.reason_code == "ineligible_task_reference"
    assert decision.reference_ids == ()


def test_malformed_explicit_task_reference_clarifies_instead_of_model_fallback():
    decision = decide_route(
        "Why did task definitely-not-a-uuid fail?",
        eligible_task_ids=(str(uuid4()),),
    )
    assert decision.action == RouteAction.CLARIFY
    assert decision.reason_code == "invalid_task_reference"


def test_gateway_uses_only_selected_verified_failed_task_observation():
    gateway, chat, runner = _gateway()
    ref_one = _prior_turn(gateway, "first failed task")
    ref_two = _prior_turn(gateway, "second failed task")
    one = str(uuid4())
    two = str(uuid4())
    candidate_one = TaskCandidate(one, ref_one)
    candidate_two = TaskCandidate(two, ref_two)
    gateway.task_history = History(
        (candidate_one, candidate_two),
        {
            one: _observation(one, ref_one),
            two: _observation(two, ref_two),
        },
    )

    gateway.turn(f"Why did task {two} fail?")

    assert chat.calls == 0
    assert len(runner.calls) == 1
    task, kwargs = runner.calls[0]
    assert kwargs["route_source"] == RouteSource.RULE
    assert kwargs["rule_id"] == RULE_TASK_DIAGNOSTIC
    assert kwargs["skill"] == "task-diagnostic"
    assert f"Task {two}" in task
    assert f"Task {one}" not in task
    assert "untrusted historical data" in task


def test_gateway_ineligible_explicit_task_id_never_reaches_model_or_worker():
    gateway, chat, runner = _gateway()
    ref = _prior_turn(gateway, "failed task")
    eligible = str(uuid4())
    gateway.task_history = History(
        (TaskCandidate(eligible, ref),),
        {eligible: _observation(eligible, ref)},
    )

    answer = gateway.turn(f"Why did task {uuid4()} fail?")

    assert "No task was run" in answer
    assert chat.calls == 0
    assert runner.calls == []
