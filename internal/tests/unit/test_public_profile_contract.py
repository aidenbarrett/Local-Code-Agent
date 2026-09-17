from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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
