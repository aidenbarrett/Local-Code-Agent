from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "session-hub.py"


def _session_hub_module():
    spec = spec_from_file_location("session_hub_endpoint_test", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "profile": "ptl-npu-8b",
        "worker_profile": None,
        "base_url": None,
        "worker_base_url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_base_url_override_is_shared_by_chat_and_worker():
    hub = _session_hub_module()

    chat, worker_profile, worker = hub._resolve_model_configs(
        _args(base_url="http://example.invalid:18000/v1")
    )

    assert worker_profile == "ptl-npu-8b"
    assert chat.base_url == "http://example.invalid:18000/v1"
    assert worker.base_url == chat.base_url


def test_worker_base_url_is_an_explicit_split_override():
    hub = _session_hub_module()

    chat, _worker_profile, worker = hub._resolve_model_configs(
        _args(
            base_url="http://chat.invalid:18000/v1",
            worker_base_url="http://worker.invalid:19000/v1",
        )
    )

    assert chat.base_url == "http://chat.invalid:18000/v1"
    assert worker.base_url == "http://worker.invalid:19000/v1"


def test_no_override_preserves_profile_defaults():
    hub = _session_hub_module()

    chat, _worker_profile, worker = hub._resolve_model_configs(_args())

    assert chat.base_url == hub.MODEL_PRESETS["ptl-npu-8b"].base_url
    assert worker.base_url == hub.MODEL_PRESETS["ptl-npu-8b"].base_url
