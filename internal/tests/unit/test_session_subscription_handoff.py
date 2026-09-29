from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService, ServiceClosed
from local_agent.session.session_store import SQLiteSessionStore


# The assertions prove handoff ordering. A loaded Windows CI runner may schedule
# either fixture thread later than a three-second rendezvous; the durable service
# itself waits up to 30 seconds for admission and completion receipts.
WAIT_SECONDS = 30


def _service(tmp_path):
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _opened(*, recovered: bool) -> dict:
    return {
        "conversation_id": "conv-1",
        "repository_id": "repo-1",
        "controller_commit": "deadbeef",
        "capabilities": ["inspect"],
        "recovered": recovered,
    }


def _sequences(events):
    return [int(event["sequence"]) for event in events]


def test_replay_subscription_bounds_history_and_delivers_new_events_live(tmp_path):
    service = _service(tmp_path)
    try:
        first = service.append("session.opened", _opened(recovered=False))
        first.wait(WAIT_SECONDS)

        handoff = service.subscribe_from(after=0, capacity=8)
        assert handoff.replay_through == 1

        second = service.append("session.opened", _opened(recovered=True))
        second.wait(WAIT_SECONDS)

        assert _sequences(service.replay_subscription(handoff)) == [1]
        assert _sequences(handoff.live.drain()) == [2]
        assert service.replay_subscription(handoff, after=handoff.replay_through) == []
    finally:
        service.close()


def test_commit_after_boundary_is_not_lost_while_handoff_holds_publish_lock(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        first = service.append("session.opened", _opened(recovered=False))
        first.wait(WAIT_SECONDS)

        # The command must already be accepted before subscribe_from takes the
        # lifecycle lock. Commands submitted afterwards are deliberately
        # serialised behind the handoff and cannot exercise the commit/publication
        # race this test is about.
        boundary_read = threading.Event()
        allow_commit = threading.Event()
        commit_done = threading.Event()
        original_append = service.store.append
        original_next_sequence = service.store.next_sequence

        def delayed_append(envelope, *, expected_sequence):
            if int(envelope["sequence"]) == 2:
                assert boundary_read.wait(WAIT_SECONDS)
                assert allow_commit.wait(WAIT_SECONDS)
            original_append(envelope, expected_sequence=expected_sequence)
            if int(envelope["sequence"]) == 2:
                commit_done.set()

        def delayed_boundary(stream_id):
            value = original_next_sequence(stream_id)
            if threading.current_thread().name == "handoff-thread":
                boundary_read.set()
                allow_commit.set()
                assert commit_done.wait(WAIT_SECONDS)
            return value

        monkeypatch.setattr(service.store, "append", delayed_append)
        monkeypatch.setattr(service.store, "next_sequence", delayed_boundary)

        second = service.append("session.opened", _opened(recovered=True))

        box = {}
        errors = []

        def open_handoff():
            try:
                box["handoff"] = service.subscribe_from(after=0, capacity=8)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=open_handoff, name="handoff-thread")
        thread.start()
        thread.join(WAIT_SECONDS)
        second.wait(WAIT_SECONDS)

        assert not errors and not thread.is_alive()
        handoff = box["handoff"]
        assert handoff.replay_through == 1
        assert _sequences(service.replay_subscription(handoff)) == [1]
        assert _sequences(handoff.live.drain()) == [2]
    finally:
        service.close()


def test_commit_before_boundary_is_replayed_once_even_if_publication_is_delayed(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        first = service.append("session.opened", _opened(recovered=False))
        first.wait(WAIT_SECONDS)

        publish_entered = threading.Event()
        release_publish = threading.Event()
        original_publish = service._publish

        def delayed_publish(event):
            if int(event["sequence"]) == 2:
                publish_entered.set()
                assert release_publish.wait(WAIT_SECONDS)
            return original_publish(event)

        monkeypatch.setattr(service, "_publish", delayed_publish)
        second = service.append("session.opened", _opened(recovered=True))
        assert publish_entered.wait(WAIT_SECONDS), "second event did not commit before handoff"

        handoff = service.subscribe_from(after=0, capacity=8)
        assert handoff.replay_through == 2
        release_publish.set()
        second.wait(WAIT_SECONDS)

        assert _sequences(service.replay_subscription(handoff)) == [1, 2]
        assert handoff.live.drain() == []
    finally:
        service.close()


def test_replay_subscription_fails_closed_for_invalid_cursor_and_closed_service(tmp_path):
    service = _service(tmp_path)
    try:
        event = service.append("session.opened", _opened(recovered=False))
        event.wait(WAIT_SECONDS)
        handoff = service.subscribe_from(after=1)
        with pytest.raises(ValueError, match="behind"):
            service.replay_subscription(handoff, after=0)
        with pytest.raises(ValueError, match="beyond"):
            service.replay_subscription(handoff, after=2)
    finally:
        service.close()

    with pytest.raises(ServiceClosed):
        service.subscribe_from(after=0)
