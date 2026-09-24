from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from textual.widgets import Static

from local_agent.config import MODEL_PRESETS
from local_agent.session.runtime_facts import RuntimeFacts
from local_agent.session.session_event_service import DurableSessionService
from local_agent.session.session_store import SQLiteSessionStore
from local_agent.session.textual_dispatch import HubTurnDispatcher
from local_agent.session.textual_feed import DurableHubFeed
from local_agent.session.textual_session_app import LiveDispatchingSessionHubApp


def _service(tmp_path) -> DurableSessionService:
    return DurableSessionService(
        SQLiteSessionStore(tmp_path / "session.db"),
        stream_id=str(uuid4()),
        session_id=str(uuid4()),
    )


def test_mounted_hub_header_reflects_observed_model_and_declared_device(tmp_path):
    async def scenario() -> None:
        service = _service(tmp_path)

        class Gateway:
            def turn(self, text, *, explicit_mode=None):
                raise AssertionError("header rendering must not dispatch a turn")

        config = replace(
            MODEL_PRESETS["ptl-npu-8b"],
            base_url="http://127.0.0.1:9999/v3",
            model="configured-model",
            device="NPU",
        )
        facts = RuntimeFacts.observe(
            "ptl-npu-8b",
            config,
            execution_enabled=False,
            fetch=lambda url: b'{"data":[{"id":"served-from-models"}]}',
        )
        dispatcher = HubTurnDispatcher(Gateway())
        app = LiveDispatchingSessionHubApp(
            DurableHubFeed(service),
            dispatcher,
            runtime_summary=facts.header(),
        )
        try:
            async with app.run_test(size=(140, 30)) as pilot:
                await pilot.pause()
                title = str(app.query_one("#title", Static).render())
                assert "served-from-models (observed)" in title
                assert "device NPU (declared)" in title
                assert "endpoint http://127.0.0.1:9999/v3" in title
                assert "execution disabled" in title
        finally:
            dispatcher.close()
            service.close()

    asyncio.run(scenario())
