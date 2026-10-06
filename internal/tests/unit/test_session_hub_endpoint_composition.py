from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

from local_agent.config import MODEL_PRESETS


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "session-hub.py"
spec = importlib.util.spec_from_file_location("session_hub_endpoint_composition", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_identical_base_urls_share_one_endpoint_authority():
    adapters = {}
    config = MODEL_PRESETS["ptl-npu-8b"]
    first = module._endpoint_adapter(config, adapters)
    second = module._endpoint_adapter(config, adapters)
    assert first is second
    assert first.runtime.endpoint_id == config.base_url.rstrip("/")
    assert len(adapters) == 1


def test_explicit_split_base_urls_get_distinct_endpoint_authorities():
    adapters = {}
    base = MODEL_PRESETS["ptl-npu-8b"]
    chat = replace(base, base_url="http://127.0.0.1:8000/v1")
    worker = replace(base, base_url="http://127.0.0.1:8001/v1")
    chat_adapter = module._endpoint_adapter(chat, adapters)
    worker_adapter = module._endpoint_adapter(worker, adapters)
    assert chat_adapter is not worker_adapter
    assert set(adapters) == {chat.base_url, worker.base_url}


def test_the_endpoint_authority_carries_the_profiles_stop_proof():
    adapters = {}
    llama = module._endpoint_adapter(MODEL_PRESETS["nuc-llama-8b"], adapters)
    assert llama.runtime.stop_proof is not None
    assert llama.runtime.stop_proof.kind == "llamacpp_metrics"
    # The 30B profile serves the same port with the same proof source: shared.
    assert module._endpoint_adapter(MODEL_PRESETS["nuc-llama-30b"], adapters) is llama
    assert module._endpoint_adapter(MODEL_PRESETS["ptl-npu-8b"], {}).runtime.stop_proof is None


def test_profiles_sharing_an_endpoint_cannot_disagree_about_its_stop_proof():
    import pytest

    adapters = {}
    module._endpoint_adapter(MODEL_PRESETS["nuc-llama-8b"], adapters)
    unproved = replace(MODEL_PRESETS["nuc-llama-8b"], stop_proof="none")
    with pytest.raises(ValueError, match="different stop_proof"):
        module._endpoint_adapter(unproved, adapters)
