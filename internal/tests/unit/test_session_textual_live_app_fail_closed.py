from __future__ import annotations

from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_live_app import LiveSessionHubApp


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_live_app_freezes_last_truthful_state_after_feed_failure(tmp_path):
    service = _service(tmp_path)
    try:
        feed = DurableHubFeed(service)
        app = LiveSessionHubApp(feed)
        before = app.view_state

        class BrokenBinding:
            def poll(self):
                from local_agent.session.textual_feed import HubFeedError

                raise HubFeedError("boom")

        app.live_binding = BrokenBinding()
        app.replace_state = lambda state: setattr(app, "view_state", state)
        app._poll_live_feed()

        assert app.view_state.conversation == before.conversation
        assert app.view_state.tasks == before.tasks
        assert app.view_state.watches == before.watches
        assert app.view_state.route_summary == before.route_summary
        assert app.view_state.status == "Durable feed error · boom"
        assert app._feed_failed is True
    finally:
        service.close()
