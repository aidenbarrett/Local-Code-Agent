from __future__ import annotations

from uuid import uuid4

from local_agent.session.conversation_store import OpenConversation, new_session
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_conversation import CanonicalConversationProvider
from local_agent.session.textual_runtime import build_textual_session_runtime


def test_public_textual_runtime_shares_canonical_conversation_and_durable_service(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )
    owned = OpenConversation(
        session=new_session("ptl-npu-8b", "fixture", "NPU", budget_chars=1000),
        _runtime_root=tmp_path,
    )

    class Gateway:
        def turn(self, text, *, explicit_mode=None):
            return "unused"

    runtime = build_textual_session_runtime(service, owned, Gateway())
    try:
        feed = runtime.app.live_binding.feed
        assert feed.service is service
        assert isinstance(feed.conversation_provider, CanonicalConversationProvider)
        assert feed.conversation_provider.owned is owned
        assert runtime.app.turn_dispatcher is runtime.dispatcher
        assert runtime.dispatcher.closed is False
    finally:
        runtime.close()
        service.close()
    assert runtime.dispatcher.closed is True


def test_public_textual_runtime_rejects_non_owned_conversation(tmp_path):
    service = DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )

    class Gateway:
        def turn(self, text, *, explicit_mode=None):
            return "unused"

    try:
        try:
            build_textual_session_runtime(service, new_session("p", "m", "d"), Gateway())
        except TypeError as exc:
            assert "OpenConversation" in str(exc)
        else:
            raise AssertionError("unowned conversation was accepted")
    finally:
        service.close()
