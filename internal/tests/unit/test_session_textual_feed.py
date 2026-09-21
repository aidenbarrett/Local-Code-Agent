from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed, HubFeedError
from local_agent.session.textual_hub import ConversationEntry


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _opened(service: DurableSessionService, label: str):
    receipt = service.append(
        "session.opened",
        {
            "conversation_id": label,
            "repository_id": "repo-1",
            "controller_commit": "fixture",
            "capabilities": [],
            "recovered": False,
        },
    )
    receipt.wait(5)


def test_start_replays_to_exact_handoff_boundary_and_projects_conversation(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "first")
        _opened(service, "second")
        feed = DurableHubFeed(
            service,
            conversation_provider=lambda: (
                ConversationEntry("user", "hello"),
                ConversationEntry("assistant", "hi"),
            ),
            replay_page=1,
        )
        state = feed.start()
        assert feed.cursor == 2
        assert [event["sequence"] for event in feed.events] == [1, 2]
        assert [entry.text for entry in state.conversation] == ["hello", "hi"]
        assert state.status == "Live · durable seq 2"
    finally:
        service.close()


def test_live_poll_accepts_only_new_committed_events(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        feed = DurableHubFeed(service)
        feed.start()
        assert feed.poll() is False

        _opened(service, "live")
        assert feed.poll() is True
        assert feed.cursor == 2
        assert feed.state().status == "Live · durable seq 2"
    finally:
        service.close()


def test_subscription_overflow_replays_from_last_accepted_cursor(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        feed = DurableHubFeed(service, capacity=1, replay_page=1)
        feed.start()

        _opened(service, "one")
        _opened(service, "two")
        assert feed.poll() is True
        assert feed.cursor == 3
        assert feed.recovered_gap is True
        assert [event["sequence"] for event in feed.events] == [1, 2, 3]
        assert "replay recovered" in feed.state().status
    finally:
        service.close()


def test_feed_fails_closed_instead_of_truncating_required_projection_history(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        _opened(service, "second")
        feed = DurableHubFeed(service, max_events=1)
        with pytest.raises(HubFeedError, match="bounded event history"):
            feed.start()
    finally:
        service.close()


def test_feed_rejects_poll_before_start(tmp_path):
    service = _service(tmp_path)
    try:
        feed = DurableHubFeed(service)
        with pytest.raises(HubFeedError, match="started"):
            feed.poll()
    finally:
        service.close()
