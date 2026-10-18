"""Stacked (medium) layout keeps the conversation the primary surface.

Below 120 columns the Hub stacks its panes. The conversation used to get a third of
the height, minus the docked composer, which at 90x28 (a large projector font) left it
one or two visible lines under a full activity pane.
"""
from __future__ import annotations

import asyncio

import pytest

from local_agent.session.textual_hub import ConversationEntry, HubViewState, SessionHubApp


@pytest.mark.parametrize("size", [(90, 28), (100, 30), (119, 40)])
def test_the_conversation_gets_the_larger_share_when_stacked(size):
    entries = tuple(ConversationEntry("user" if i % 2 == 0 else "assistant", f"line {i}")
                    for i in range(30))

    async def scenario():
        app = SessionHubApp(HubViewState(conversation=entries))
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            conversation = app.query_one("#conversation-pane").region
            side = app.query_one("#side-column").region
            assert conversation.height >= side.height, "the conversation is smaller than the side panes"
            assert conversation.height >= 6, "the conversation shows only a line or two"
            assert app.query_one("#composer").region.height == 3
    asyncio.run(scenario())
