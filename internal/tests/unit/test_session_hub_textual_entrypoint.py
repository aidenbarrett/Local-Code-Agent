from __future__ import annotations

from uuid import uuid4

from local_agent.session.conversation_store import new_session
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_conversation import CanonicalConversationProvider
from local_agent.session.textual_dispatch import HubTurnDispatcher
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_session_app import LiveDispatchingSessionHubApp


class _OwnedFixture:
    def __init__(self):
        self.session = new_session("ptl-npu-8b", "fixture", "NPU", budget_chars=1000)


def test_public_textual_components_share_canonical_conversation_and_durable_service(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )

    class Gateway:
        def turn(self, text, *, explicit_mode=None):
            return "unused"

    dispatcher = HubTurnDispatcher(Gateway())
    try:
        owned = _OwnedFixture()
        provider = CanonicalConversationProvider.__new__(CanonicalConversationProvider)
        provider.owned = owned
        feed = DurableHubFeed(service, conversation_provider=provider)
        app = LiveDispatchingSessionHubApp(feed, dispatcher)
        assert app.live_binding.feed.service is service
        assert app.live_binding.feed.conversation_provider is provider
        assert app.turn_dispatcher is dispatcher
    finally:
        dispatcher.close()
        service.close()
