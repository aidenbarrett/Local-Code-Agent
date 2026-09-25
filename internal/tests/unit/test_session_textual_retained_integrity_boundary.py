from __future__ import annotations

from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed


def test_feed_keeps_empty_authoritative_projection_when_no_retained_results(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    try:
        receipt = service.append(
            "session.opened",
            {
                "conversation_id": "conv-1",
                "repository_id": "repo-1",
                "controller_commit": "fixture",
                "capabilities": [],
                "recovered": False,
            },
        )
        receipt.wait(5)
        state = DurableHubFeed(service).start()
        assert state.tasks == ()
        assert state.status == "Live · durable seq 1"
    finally:
        service.close()
