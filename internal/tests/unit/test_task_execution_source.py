from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.contracts import RouteSource, TaskOutcome
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.execution_source import TaskExecutionSource
from local_agent.session.task_controller import TaskController


def test_conversation_route_sources_map_into_execution_provenance_without_expansion():
    assert TaskExecutionSource.coerce(RouteSource.RULE) == TaskExecutionSource.RULE
    assert TaskExecutionSource.coerce(RouteSource.MODEL_PROPOSAL) == TaskExecutionSource.MODEL_PROPOSAL
    assert TaskExecutionSource.coerce(RouteSource.USER_DIRECT) == TaskExecutionSource.USER_DIRECT
    assert TaskExecutionSource.coerce("watch") == TaskExecutionSource.WATCH
    with pytest.raises(ValueError):
        RouteSource("watch")


def test_controller_accepts_watch_execution_without_manufacturing_conversation_route(loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    events = EventBuffer("execution-source-test")

    def unavailable_worker():
        raise RuntimeError("worker intentionally unavailable")

    controller = TaskController(repo, unavailable_worker, events, allow_execution=False)
    task_id = str(uuid4())
    result = controller.run("inspect", route_source=TaskExecutionSource.WATCH, task_id=task_id)

    assert result.task_id == task_id
    assert result.outcome == TaskOutcome.NO_VERDICT
    emitted = events.after(0)
    assert emitted[0].kind == "task.started"
    assert emitted[0].payload["route_source"] == "watch"
    assert emitted[-1].kind == "task.interrupted"


def test_controller_rejects_unknown_execution_source_before_task_effects(loaded):
    _sandbox, repo, _registry, _store, _skills = loaded
    events = EventBuffer("execution-source-invalid")
    controller = TaskController(repo, lambda: None, events, allow_execution=False)

    with pytest.raises(ValueError):
        controller.run("inspect", route_source="scheduler-ish", task_id=str(uuid4()))
    assert events.after(0) == []
