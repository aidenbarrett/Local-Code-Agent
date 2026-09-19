"""Regressions for the adversarial review of the conversation-gateway branch.

Each test names the defect it pins. They are written as the defect, so a revert
fails them by name rather than by a vague assertion somewhere downstream.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from local_agent.session.contracts import TaskResult
from local_agent.session.event_buffer import EventBuffer
from local_agent.session.conversation_gateway import ConversationGateway


class _Chat:
    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None):
        self.seen = messages
        return SimpleNamespace(content=self.replies.pop(0), tool_calls=None,
                               stats=SimpleNamespace(as_dict=lambda: {}))


class _Controller:
    def __init__(self):
        self.calls = []

    def run(self, task, self_check=False, **kwargs):
        self.calls.append(task)
        return TaskResult("t1", "fail", "The build broke.", False,
                          ("read_file:0", "run_test:1"), {})


def test_event_sequences_stay_unique_under_concurrent_emit():
    """`self._sequence += 1` is load-add-store. Gap detection in after() depends
    on unique monotonic sequences, and telemetry samplers emit from a thread."""
    events = EventBuffer("s", capacity=100_000)

    def spam():
        for _ in range(3000):
            events.emit("x", {})

    threads = [threading.Thread(target=spam) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    sequences = [event.sequence for event in events.after(0)]
    assert len(sequences) == 12_000
    assert len(set(sequences)) == 12_000, "duplicate event sequence numbers"


def test_every_event_carries_wall_clock_as_well_as_monotonic_time():
    """Monotonic time orders events. It cannot say "03:00 last night"."""
    event = EventBuffer("s").emit("x", {})
    assert event.monotonic_s > 0
    assert event.wall_utc.endswith("Z") and "T" in event.wall_utc


def test_the_conversation_model_is_never_shown_the_controller_verdict():
    """It has no need for the verdict line and every opportunity to paraphrase
    it into a later reply."""
    controller = _Controller()
    gateway = ConversationGateway(
        _Chat(['{"kind":"repository","text":"inspect"}',
               '{"kind":"reply","text":"grand"}']),
        controller, EventBuffer("s"))

    shown_to_user = gateway.turn("what broke?")
    assert "[Controller: fail" in shown_to_user

    gateway.turn("and now?")
    replayed = json.dumps(gateway.chat_client.seen)
    assert "The build broke." in replayed, "the model lost the substance of the task"
    assert "[Controller:" not in replayed, "the model was shown the verdict"


def test_the_verdict_reports_how_much_evidence_there_was():
    """A claim nobody can count is not a checkable claim."""
    rendered = TaskResult("t1", "pass", "Done.", True, ("a:0", "b:1")).render()
    assert "evidence: 2 item(s)" in rendered


def test_interrupt_has_one_terminal_event_and_is_not_swallowed(loaded):
    import pytest
    from local_agent.session.task_controller import TaskController
    _, repo, *_ = loaded
    events = EventBuffer("s")
    def interrupt():
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        TaskController(repo, interrupt, events).run("inspect repository")
    assert [e.kind for e in events.after(0)] == ["task.started", "task.interrupted"]
    assert events.after(0)[-1].payload["process_cleanup_confirmed"] is False


def test_blocked_reason_is_projected_without_tool_arguments(loaded, monkeypatch):
    import local_agent.session.task_controller as module
    _, repo, *_ = loaded
    events = EventBuffer("s")
    class Worker:
        def __init__(self, **kwargs):
            self.observe = kwargs["observer"]
        def run(self, task):
            self.observe("blocked", {"reason": "build_target could not run (policy)",
                                     "skill": "build", "args": {"secret": "SECRET"}})
            raise RuntimeError("SECRET")
    monkeypatch.setattr(module, "Orchestrator", Worker)
    result = module.TaskController(repo, lambda: None, events).run("build")
    blocked = next(e for e in events.after(0) if e.kind == "worker.blocked")
    assert blocked.payload == {"reason": "build_target could not run (policy)", "skill": "build"}
    assert "SECRET" not in result.render()
    assert all("SECRET" not in e.payload_json for e in events.after(0))


def test_profile_budget_has_separate_character_and_byte_units():
    from local_agent.session.cli import conversation_budgets
    budgets = conversation_budgets(7500)
    assert budgets == {"history_chars": 16000, "request_chars": 24000, "request_bytes": 97024}
    gateway = ConversationGateway(None, None, EventBuffer("s"), **budgets)
    # Same character policy for ASCII and four-byte Unicode. Backend token
    # limits remain independent of this approximate sizing policy.
    assert not gateway._over_budget([{"role": "user", "content": "x" * 24000}])
    assert not gateway._over_budget([{"role": "user", "content": "😀" * 24000}])
    assert gateway._over_budget([{"role": "user", "content": "x" * 24001}])
    byte_limited = ConversationGateway(None, None, EventBuffer("s"), request_bytes=100)
    assert not byte_limited._over_budget([{"role": "user", "content": "x" * 30}])
    assert byte_limited._over_budget([{"role": "user", "content": "😀" * 30}])


def test_budget_refusal_is_recorded_without_model_or_controller_call():
    from local_agent.session.conversation_gateway import SYSTEM
    chat = _Chat(['{"kind":"reply","text":"hello"}'])
    controller = _Controller()
    events = EventBuffer("s")
    gateway = ConversationGateway(
        chat,
        controller,
        events,
        history_chars=1,
        request_chars=len(SYSTEM) + 5,
    )
    said = "message too long"
    answer = gateway.turn(said)
    assert gateway._history == [(said, answer)]
    assert not hasattr(chat, "seen") and not controller.calls
    assert [e.kind for e in events.after(0)] == ["turn.started", "turn.refused", "turn.finished"]
    assert events.after(0)[1].payload == {"reason": "context_budget", "task_started": False}
    # A later short turn still runs; refused context is subject to normal eviction.
    assert "hello" in gateway.turn("hi")
    assert not controller.calls


def test_slow_sink_does_not_block_snapshot_and_delivery_remains_ordered():
    entered, release = threading.Event(), threading.Event()
    seen = []
    def sink(event):
        if event.sequence == 1:
            entered.set()
            assert release.wait(3)
        seen.append(event.sequence)
    events = EventBuffer("s", sink)
    first = threading.Thread(target=lambda: events.emit("first", {}))
    second = threading.Thread(target=lambda: events.emit("second", {}))
    first.start()
    assert entered.wait(3)
    second.start()
    try:
        assert [e.sequence for e in events.after(0)] == [1]
    finally:
        release.set()
        first.join(3)
        second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert seen == [1, 2]
    assert events.delivery_errors == 0