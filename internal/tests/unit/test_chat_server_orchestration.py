from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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


def _render(monkeypatch, tmp_path, outcome, steps=()):
    """Run chat's presenter against a stubbed owner; return (ok, transcript)."""
    from io import StringIO

    from terminal_ui import ui

    chat = _chat()
    profile, _, config = _config(chat)
    calls = []

    def ensure(seen_profile, seen_config, root, *, progress):
        calls.append((seen_profile, seen_config, root))
        for step in steps:
            progress(step)
        return outcome

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat, "ensure_managed_runtime", ensure)
    stream = StringIO()
    ok = chat._ensure_server(profile, config, term=ui(stream=stream, colour=False))
    assert calls == [(profile, config, tmp_path)], "chat must delegate to the one owner"
    return ok, stream.getvalue()


def test_chat_has_no_server_decision_logic_of_its_own():
    source = (INTERNAL / "scripts" / "chat.py").read_text(encoding="utf-8")
    for duplicate in ("serve.start(", "serve.stop(", "serve.read_record(", "def _reachable("):
        assert duplicate not in source, duplicate


def test_chat_reports_a_reused_server(monkeypatch, tmp_path):
    from serving.managed_runtime import RuntimeEnsureResult, RuntimeLifecycle

    ok, text = _render(monkeypatch, tmp_path, RuntimeEnsureResult(RuntimeLifecycle.REUSED, "x"))
    assert ok is True
    assert "already ready" in text and "Starting" not in text


def test_chat_announces_stop_and_start_before_a_replacement(monkeypatch, tmp_path):
    from serving.managed_runtime import (
        ReplacedReason,
        RuntimeEnsureResult,
        RuntimeLifecycle,
        RuntimeStep,
    )

    outcome = RuntimeEnsureResult(RuntimeLifecycle.REPLACED, "x",
                                  replaced_reason=ReplacedReason.CONFIG_CHANGED,
                                  changed_fields=("args",), launch_to_ready_ms=10)
    ok, text = _render(monkeypatch, tmp_path, outcome,
                       steps=(RuntimeStep.STOPPING, RuntimeStep.STARTING))
    assert ok is True
    assert text.index("Stopping") < text.index("Starting") < text.index("Model server ready")


def test_chat_refuses_to_adopt_unmanaged_server(monkeypatch, tmp_path):
    from serving.managed_runtime import RefusedReason, RuntimeEnsureResult, RuntimeLifecycle

    outcome = RuntimeEnsureResult(RuntimeLifecycle.REFUSED, "reachable but not owned",
                                  refused_reason=RefusedReason.FOREIGN_ENDPOINT)
    ok, text = _render(monkeypatch, tmp_path, outcome)
    assert ok is False
    assert "not owned by Local Code Agent" in text and "install.ps1" not in text


def test_chat_reports_a_failed_start_with_the_setup_step(monkeypatch, tmp_path):
    from serving.managed_runtime import RefusedReason, RuntimeEnsureResult, RuntimeLifecycle

    outcome = RuntimeEnsureResult(RuntimeLifecycle.REFUSED, "managed model endpoint could not be started: boom",
                                  refused_reason=RefusedReason.START_FAILED)
    ok, text = _render(monkeypatch, tmp_path, outcome)
    assert ok is False
    assert "could not be started: boom" in text and "install.ps1" in text
