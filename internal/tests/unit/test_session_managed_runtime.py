from __future__ import annotations

from local_agent.config import MODEL_PRESETS
from serving import managed_runtime


def test_session_refuses_unowned_reachable_endpoint(monkeypatch, tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    plan = object()
    monkeypatch.setattr(managed_runtime.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(managed_runtime.serve, "read_record", lambda p: None)
    monkeypatch.setattr(managed_runtime, "endpoint_reachable", lambda c: True)
    result = managed_runtime.ensure_managed_runtime("ptl-npu-8b", config, tmp_path)
    assert result.ok is False
    assert "not owned" in result.message
    assert result.refused_reason is managed_runtime.RefusedReason.FOREIGN_ENDPOINT
