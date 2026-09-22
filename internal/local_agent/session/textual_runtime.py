"""Owned runtime composition for the live Textual Session Hub.

The public composition root supplies the already-owned conversation, durable service and
gateway. This module joins those existing authorities without creating another store or
controller. Closing the runtime stops new turns and waits for any already-started turn;
it does not pretend that UI exit cancelled execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from .conversation_store import OpenConversation
from .session_event_service import DurableSessionService
from .textual_conversation import CanonicalConversationProvider
from .textual_dispatch import HubTurnDispatcher
from .textual_feed import DurableHubFeed
from .textual_session_app import LiveDispatchingSessionHubApp


@dataclass
class TextualSessionRuntime:
    app: LiveDispatchingSessionHubApp
    dispatcher: HubTurnDispatcher

    def close(self) -> None:
        self.dispatcher.close(wait=True)

    def __enter__(self) -> "TextualSessionRuntime":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


def build_textual_session_runtime(
    service: DurableSessionService,
    conversation: OpenConversation,
    gateway,
    *,
    palette_name: str = "neon",
    poll_interval: float = 0.10,
    completion_poll_interval: float = 0.05,
) -> TextualSessionRuntime:
    """Compose one UI from the canonical conversation, durable stream and gateway."""
    if not isinstance(service, DurableSessionService):
        raise TypeError("Textual Session Hub runtime requires DurableSessionService")
    if not isinstance(conversation, OpenConversation):
        raise TypeError("Textual Session Hub runtime requires OpenConversation")

    provider = CanonicalConversationProvider(conversation)
    feed = DurableHubFeed(service, conversation_provider=provider)
    dispatcher = HubTurnDispatcher(gateway)
    try:
        app = LiveDispatchingSessionHubApp(
            feed,
            dispatcher,
            palette_name=palette_name,
            poll_interval=poll_interval,
            completion_poll_interval=completion_poll_interval,
        )
    except BaseException:
        dispatcher.close(wait=True)
        raise
    return TextualSessionRuntime(app=app, dispatcher=dispatcher)


__all__ = ["TextualSessionRuntime", "build_textual_session_runtime"]
