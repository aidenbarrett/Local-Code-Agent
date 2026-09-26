from __future__ import annotations

from local_agent.config import MODEL_PRESETS
from serving import managed_runtime


def test_session_reuses_matching_owned_runtime(monkeypatch, tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    plan = object()
    record = {"plan": {"model_configuration": {"device": config.device, "model": config.model}}}
    calls = []
    monkeypatch.setattr(managed_runtime.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(managed_runtime.serve, "read_record", lambda p: record)
    monkeypatch.setattr(managed_runtime.serve, "status", lambda p: {"healthy": True, "process_alive": True})
    monkeypatch.setattr(managed_runtime.serve, "stop", lambda p: calls.append("stop"))
    monkeypatch.setattr(managed_runtime.serve, "start", lambda *a, **k: calls.append("start") or {"healthy": True})
    result = managed_runtime.ensure_managed_runtime("ptl-npu-8b", config, tmp_path)
    assert result.ok is True
    assert calls == []


def test_session_stops_only_stale_owned_runtime_before_start(monkeypatch, tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    plan = object()
    record = {"plan": {"model_configuration": {"device": "GPU", "model": config.model}}}
    calls = []
    monkeypatch.setattr(managed_runtime.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(managed_runtime.serve, "read_record", lambda p: record)
    monkeypatch.setattr(managed_runtime.serve, "status", lambda p: {"healthy": True, "process_alive": True})
    monkeypatch.setattr(managed_runtime.serve, "stop", lambda p: calls.append("stop"))
    monkeypatch.setattr(managed_runtime, "endpoint_reachable", lambda c: False)
    monkeypatch.setattr(managed_runtime.serve, "start", lambda p, c, wait_seconds: calls.append("start") or {"healthy": True})
    result = managed_runtime.ensure_managed_runtime("ptl-npu-8b", config, tmp_path)
    assert result.ok is True
    assert calls == ["stop", "start"]


def test_session_refuses_unowned_reachable_endpoint(monkeypatch, tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    plan = object()
    monkeypatch.setattr(managed_runtime.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(managed_runtime.serve, "read_record", lambda p: None)
    monkeypatch.setattr(managed_runtime, "endpoint_reachable", lambda c: True)
    result = managed_runtime.ensure_managed_runtime("ptl-npu-8b", config, tmp_path)
    assert result.ok is False
    assert "not owned" in result.message
