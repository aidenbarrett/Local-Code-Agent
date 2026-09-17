from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


INTERNAL = Path(__file__).resolve().parents[2]


def _chat():
    path = INTERNAL / "scripts" / "chat.py"
    spec = spec_from_file_location("chat_server_orchestration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(chat, name="qwen3-8b-npu"):
    resolved = chat._resolve(name)
    assert resolved is not None
    return resolved


def test_chat_reuses_owned_compatible_server(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    record = {"plan": {"model_configuration": {"device": "NPU", "model": config.model}}}

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: record)
    monkeypatch.setattr(chat.serve, "status", lambda p: {"healthy": True, "process_alive": True})
    monkeypatch.setattr(chat.serve, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))

    assert chat._ensure_server(profile, config) is True


def test_chat_stops_owned_wrong_device_before_starting_requested_device(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    record = {"plan": {"model_configuration": {"device": "GPU", "model": config.model}}}
    events = []

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: record)
    monkeypatch.setattr(chat.serve, "status", lambda p: {"healthy": True, "process_alive": True})
    monkeypatch.setattr(chat.serve, "stop", lambda p: events.append("stop"))
    monkeypatch.setattr(chat.serve, "start", lambda *a, **k: events.append("start") or {"healthy": True})

    assert chat._ensure_server(profile, config) is True
    assert events == ["stop", "start"]


def test_chat_refuses_to_adopt_unmanaged_server(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    started = []

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: None)
    monkeypatch.setattr(chat, "_reachable", lambda c: True)
    monkeypatch.setattr(chat.serve, "start", lambda *a, **k: started.append(True))

    assert chat._ensure_server(profile, config) is False
    assert started == []


def test_chat_starts_requested_server_when_endpoint_is_free(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    calls = []

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: None)
    monkeypatch.setattr(chat, "_reachable", lambda c: False)
    monkeypatch.setattr(chat.serve, "start", lambda p, c, wait_seconds: calls.append((c.device, wait_seconds)) or {"healthy": True})

    assert chat._ensure_server(profile, config) is True
    assert calls == [("NPU", 900)]



def test_chat_refuses_unmanaged_server_even_with_a_stale_ownership_record(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    record = {"plan": {"model_configuration": {"device": "NPU", "model": config.model}}}
    started = []

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: record)
    monkeypatch.setattr(chat.serve, "status", lambda p: {"healthy": False, "process_alive": False})
    monkeypatch.setattr(chat, "_reachable", lambda c: True)
    monkeypatch.setattr(chat.serve, "start", lambda *a, **k: started.append(True))

    assert chat._ensure_server(profile, config) is False
    assert started == []
