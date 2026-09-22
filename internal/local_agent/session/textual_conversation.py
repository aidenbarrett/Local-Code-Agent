"""Canonical conversation projection for the Textual Session Hub.

Raw user/assistant prose remains owned by ``conversation_store``. This module exposes
that committed in-memory conversation as immutable presentation values without
inventing system turns, task results, verdict prose, or route authority.
"""
from __future__ import annotations

from .conversation_store import OpenConversation, Session
from .textual_hub import ConversationEntry


class ConversationProjectionError(RuntimeError):
    """Canonical conversation state cannot be projected truthfully."""


def project_conversation(session: Session) -> tuple[ConversationEntry, ...]:
    """Project the exact stored raw turns in append order.

    Stored conversation schema v1 permits only user/assistant turns. Re-check that
    boundary here because presentation must fail closed rather than render a malformed
    role as trusted UI state.
    """
    if not isinstance(session, Session):
        raise TypeError("Textual conversation projection requires Session")

    entries: list[ConversationEntry] = []
    for index, turn in enumerate(tuple(session.turns)):
        if turn.role not in {"user", "assistant"}:
            raise ConversationProjectionError(
                f"stored conversation turn {index} has unsupported role {turn.role!r}"
            )
        if not isinstance(turn.content, str):
            raise ConversationProjectionError(
                f"stored conversation turn {index} has non-string content"
            )
        entries.append(ConversationEntry(turn.role, turn.content))
    return tuple(entries)


class CanonicalConversationProvider:
    """Callable live-feed provider over the owned canonical conversation object."""

    def __init__(self, owned: OpenConversation) -> None:
        if not isinstance(owned, OpenConversation):
            raise TypeError("canonical Textual provider requires OpenConversation")
        self.owned = owned

    def __call__(self) -> tuple[ConversationEntry, ...]:
        return project_conversation(self.owned.session)


__all__ = [
    "CanonicalConversationProvider",
    "ConversationProjectionError",
    "project_conversation",
]
