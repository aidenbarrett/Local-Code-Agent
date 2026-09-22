from __future__ import annotations

import ast
from pathlib import Path


def test_public_session_hub_launches_textual_runtime_without_plain_input_loop():
    source = Path("internal/scripts/session-hub.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    assert "build_textual_session_runtime" in source
    assert "textual.app.run()" in source
    assert not any(isinstance(call.func, ast.Name) and call.func.id == "input" for call in calls)


def test_public_session_hub_does_not_attach_transient_print_sink_behind_textual():
    source = Path("internal/scripts/session-hub.py").read_text(encoding="utf-8")
    assert "events = EventBuffer(service.stream_id)" in source
    assert "EventBuffer(service.stream_id, show)" not in source
