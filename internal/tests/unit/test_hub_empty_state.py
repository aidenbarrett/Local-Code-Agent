"""A fresh Hub invites a first request instead of showing an empty form."""
from __future__ import annotations

from local_agent.session.textual_hub import (
    load_palette,
    render_conversation,
    render_conversation_text,
)

PALETTE = load_palette("neon")


def test_the_empty_conversation_invites_a_first_request():
    plain = render_conversation(())
    assert plain.startswith("No conversation yet. Ask in plain words")
    assert "what can you do?" in plain and "run the tests" in plain
    assert render_conversation_text((), (), PALETTE).plain == plain
