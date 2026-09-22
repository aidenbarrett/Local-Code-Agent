from __future__ import annotations

import ast
from pathlib import Path


def _session_source() -> str:
    return Path("internal/scripts/session-hub.py").read_text(encoding="utf-8")


def test_public_session_hub_launches_textual_runtime_without_plain_input_loop():
    source = _session_source()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    assert "build_textual_session_runtime" in source
    assert "textual.app.run()" in source
    assert not any(isinstance(call.func, ast.Name) and call.func.id == "input" for call in calls)


def test_public_session_hub_does_not_attach_transient_print_sink_behind_textual():
    source = _session_source()
    assert "events = EventBuffer(service.stream_id)" in source
    assert "EventBuffer(service.stream_id, show)" not in source


def test_public_session_hub_composes_durable_admission_before_gateway_dispatch():
    source = _session_source()
    admitted = source.index("admitted_controller = AdmittedDurableTaskController(service, controller)")
    runner = source.index("task_runner = DurableTaskAdmissionRunner(")
    gateway = source.index("gateway = ConversationGateway(")
    textual = source.index("build_textual_session_runtime(service, opened, gateway)")

    assert admitted < runner < gateway < textual
    assert "task_runner=task_runner" in source
