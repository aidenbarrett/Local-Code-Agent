from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


INTERNAL = Path(__file__).resolve().parents[2]


def _chat():
    path = INTERNAL / "scripts" / "chat.py"
    spec = spec_from_file_location("run_task_server_start_chat", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hidden_ensure_only_path_prepares_public_npu_profile(monkeypatch):
    chat = _chat()
    calls = []

    def ensure(profile, config, term=None):
        calls.append((profile, config.device, term))
        return True

    monkeypatch.setattr(chat, "_ensure_server", ensure)

    assert chat.main(["qwen3-8b-npu", "--ensure-only"]) == 0
    assert calls == [("ptl-npu-8b", "NPU", None)]


def test_hidden_ensure_only_path_propagates_start_failure(monkeypatch):
    chat = _chat()
    monkeypatch.setattr(chat, "_ensure_server", lambda *args, **kwargs: False)

    assert chat.main(["qwen3-8b-npu", "--ensure-only"]) == 2
