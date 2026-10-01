from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from io import StringIO


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"


def _load_chat_module():
    script = INTERNAL / "scripts" / "chat.py"
    spec = spec_from_file_location("public_profile_contract_chat", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_friendly_chat_profile_resolves_to_live_model_configuration():
    chat = _load_chat_module()

    for friendly, (profile, device) in chat.FRIENDLY.items():
        assert profile in chat.MODEL_PRESETS, (
            f"public chat choice {friendly!r} points at missing MODEL_PRESETS profile {profile!r}"
        )
        resolved = chat._resolve(friendly)
        assert resolved is not None, f"public chat choice {friendly!r} no longer resolves"
        resolved_profile, resolved_device, config = resolved
        assert resolved_profile == profile
        assert resolved_device == device
        assert config.device == device


def test_every_local_preset_is_a_direct_chat_choice():
    chat = _load_chat_module()

    for profile in chat.local_presets():
        resolved = chat._resolve(profile)
        assert resolved is not None
        resolved_profile, resolved_device, config = resolved
        assert resolved_profile == profile
        assert resolved_device == config.device


def test_chat_list_shows_local_presets_with_weight_state(tmp_path, monkeypatch):
    chat = _load_chat_module()
    stream = StringIO()
    make_ui = chat.ui
    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat, "ui", lambda: make_ui(stream=stream, colour=False))
    monkeypatch.setattr(
        chat,
        "weight_state",
        lambda profile, root: "downloaded" if profile == "ptl-npu-8b" else "missing",
    )

    assert chat.main(["list"]) == 0
    output = stream.getvalue()
    for profile in chat.local_presets():
        assert profile in output
    assert "ptl-npu-8b" in output and "downloaded" in output
    assert "ptl-gpu-30b" in output and "missing" in output
    assert "qwen3-8b-npu" in output


def test_chat_accepts_preset_and_alias_and_refuses_unknown(monkeypatch, capsys):
    chat = _load_chat_module()
    calls = []
    monkeypatch.setattr(
        chat,
        "converse",
        lambda name, profile, config, persona, conversation_id=None: calls.append(
            (name, profile, config.device)
        ) or 0,
    )

    assert chat.main(["ptl-gpu-30b"]) == 0
    assert calls[-1] == ("ptl-gpu-30b", "ptl-gpu-30b", "GPU")
    assert chat.main(["qwen3-8b-cpu"]) == 0
    assert calls[-1] == ("qwen3-8b-cpu", "ptl-npu-8b", "CPU")
    assert chat.main(["not-a-preset"]) == 2
    assert "Unknown model choice" in capsys.readouterr().err
