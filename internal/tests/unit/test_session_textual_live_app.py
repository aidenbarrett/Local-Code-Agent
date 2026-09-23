from __future__ import annotations

from uuid import uuid4

from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_hub import ConversationEntry, SessionHubApp
from local_agent.session.textual_live_app import HubLiveBinding, LiveSessionHubApp


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def _opened(service: DurableSessionService, label: str) -> None:
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


def test_live_binding_starts_from_durable_replay_and_advances_on_commit(tmp_path):
    service = _service(tmp_path)
    try:
        _opened(service, "initial")
        binding = HubLiveBinding(DurableHubFeed(service))
        assert binding.state.status == "Live · durable seq 1"

        _opened(service, "second")
        state = binding.poll()
        assert state is not None
        assert state.status == "Live · durable seq 2"
        assert binding.state == state
        assert binding.poll() is None
    finally:
        service.close()


def test_live_binding_refreshes_canonical_conversation_without_fake_durable_event(tmp_path):
    service = _service(tmp_path)
    conversation = [ConversationEntry("user", "one")]
    try:
        binding = HubLiveBinding(
            DurableHubFeed(service, conversation_provider=lambda: tuple(conversation))
        )
        assert [entry.text for entry in binding.state.conversation] == ["one"]

        conversation.append(ConversationEntry("assistant", "two"))
        state = binding.poll()
        assert state is not None
        assert [entry.text for entry in state.conversation] == ["one", "two"]
    finally:
        service.close()


def test_live_app_remains_a_presentation_subclass_and_validates_poll_interval(tmp_path):
    service = _service(tmp_path)
    try:
        feed = DurableHubFeed(service)
        app = LiveSessionHubApp(feed, poll_interval=0.25)
        assert isinstance(app, SessionHubApp)
        assert app.view_state.status == "Live · durable seq 0"
        assert app.poll_interval == 0.25
    finally:
        service.close()
