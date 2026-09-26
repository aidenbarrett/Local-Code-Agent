from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "internal" / "perf" / "endpoint_harness.py"


def _module():
    spec = spec_from_file_location("endpoint_harness_under_test", HARNESS)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _profile(module):
    return module.Profile(name="test", base_url="http://127.0.0.1:9000/v1/", model="m", runtime="r", device="d")


def test_profile_requires_backend_identity(tmp_path):
    module = _module()
    path = tmp_path / "profile.toml"
    path.write_text('[endpoint]\nbase_url="http://127.0.0.1:9000/v1"\nmodel="m"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="identity.runtime"):
        module.load_profile(path)


def test_profile_keeps_backend_specific_details_in_local_config(tmp_path):
    module = _module()
    path = tmp_path / "profile.toml"
    path.write_text(
        '\n'.join([
            'name="local"', '[endpoint]', 'base_url="http://127.0.0.1:9000/v1"', 'model="m"',
            '[identity]', 'runtime="runtime-1"', 'device="device-7"',
            '[startup]', 'command=["runtime", "serve"]',
            '[telemetry]', 'prefill_time_path="timing.prefill_ms"',
            '[cancellation]', 'active_requests_url="http://127.0.0.1:9000/active"',
            'active_request_ids_path="requests"', 'request_id_header="x-request-id"',
        ]) + '\n', encoding="utf-8",
    )
    profile = module.load_profile(path)
    assert profile.base_url == "http://127.0.0.1:9000/v1/"
    assert profile.start_command == ("runtime", "serve")
    assert profile.runtime == "runtime-1"
    assert profile.device == "device-7"
    assert profile.prefill_time_path == "timing.prefill_ms"


def test_cancellation_refuses_to_guess_without_endpoint_observation():
    module = _module()
    result = module.benchmark_cancellation(object(), _profile(module))
    assert result["supported"] is False
    assert "endpoint-side" in result["reason"]


def test_context_ladder_stops_at_first_backend_rejection():
    module = _module()

    class Client:
        def __init__(self): self.calls = 0
        def stream_chat(self, prompt, max_tokens):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("context cap")
            return {"prompt_tokens": 1900, "ttft_ms": 12.0}

    client = Client()
    rows = module.benchmark_context_ladder(client, _profile(module), [2048, 8192, 32768])
    assert len(rows) == 2
    assert rows[0]["supported"] is True
    assert rows[0]["client_observed_prompt_to_first_token_ms"] == 12.0
    assert rows[1]["supported"] is False
    assert client.calls == 2


def test_cold_start_refuses_false_cold_when_endpoint_is_already_ready():
    module = _module()
    profile = module.Profile(
        name="test", base_url="http://127.0.0.1:9000/v1/", model="m", runtime="r", device="d",
        start_command=("runtime", "serve"),
    )

    class Client:
        def ready(self): return True

    result, process = module.start_for_cold_measurement(Client(), profile, 1.0)
    assert result["supported"] is False
    assert "cold state cannot be established" in result["reason"]
    assert process is None


def test_decode_rate_counts_tokens_after_first_arrival():
    source = HARNESS.read_text(encoding="utf-8")
    assert "(completion_tokens - 1) / decode_seconds" in source


def test_harness_has_no_backend_brand_names():
    text = HARNESS.read_text(encoding="utf-8")
    assert re.search(r"\b(openvino|ovms|intel|npu)\b", text, re.IGNORECASE) is None
