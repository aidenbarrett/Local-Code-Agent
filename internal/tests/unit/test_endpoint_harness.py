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


class _FakeEndpoint:
    """Scripted endpoint: each snapshot pops the next (ready, model_ids, instance) state."""

    def __init__(self, states, memory=None):
        self.states = list(states)
        self.current = self.states[0]
        self.prompts = []
        self.memory = memory

    def _advance(self):
        if self.states:
            self.current = self.states.pop(0)

    def ready(self):
        self._advance()
        return self.current[0]

    def get_json(self, url):
        if url.endswith("/models"):
            return {"data": [{"id": item} for item in self.current[1]]}
        if url.endswith("/identity"):
            return {"instance": self.current[2]}
        if url.endswith("/telemetry"):
            return {"memory": self.memory}
        raise AssertionError(url)

    def stream_chat(self, prompt, max_tokens):
        self.prompts.append(prompt)
        return {"ttft_ms": 5.0, "request_elapsed_ms": 9.0}


class _Process:
    def __init__(self, rc=None):
        self.rc = rc

    def poll(self):
        return self.rc


def _sequence(module, endpoint, *, cold, process=None, instance=False, memory=False):
    profile = module.Profile(
        name="t", base_url="http://127.0.0.1:9000/v1/", model="m", runtime="r", device="d",
        identity_url="http://127.0.0.1:9000/identity" if instance else None,
        identity_instance_path="instance" if instance else None,
        telemetry_url="http://127.0.0.1:9000/telemetry" if memory else None,
        memory_path="memory" if memory else None,
    )
    module.benchmark_generation = lambda client, prompt, max_tokens: client.stream_chat(prompt, max_tokens)
    return module.measure_lifecycle_sequence(endpoint, profile, module.LifecycleRun(
        prompt="p", max_tokens=8, nonce="abcd", established_cold=cold, process=process,
    ))


def test_warm_is_claimed_only_when_owned_process_stayed_alive():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3)
    lifecycle, first, warm = _sequence(module, endpoint, cold=True, process=_Process())
    assert lifecycle["provenance"] == "harness_established_cold_start"
    assert first["sequence_label"] == "first_after_established_cold_start"
    assert warm["sequence_label"] == "same_endpoint_warm"
    assert warm["endpoint_process_reused"] is True
    assert warm["reuse_evidence"] == "harness_owned_process_alive"


def test_pre_existing_endpoint_first_request_is_never_called_cold():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3)
    lifecycle, first, warm = _sequence(module, endpoint, cold=False)
    assert lifecycle["provenance"] == "pre_existing_endpoint"
    assert first["sequence_label"] == "first_observed_request"
    # No owned process and no instance hook: reuse is unproven, not assumed.
    assert warm["sequence_label"] == "subsequent_request_endpoint_reuse_unproven"
    assert warm["endpoint_process_reused"] is None


def test_endpoint_instance_hook_proves_reuse_without_owning_the_process():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), "boot-1")] * 3)
    _lifecycle, _first, warm = _sequence(module, endpoint, cold=False, instance=True)
    assert warm["sequence_label"] == "same_endpoint_warm"
    assert warm["reuse_evidence"] == "endpoint_reported_instance_unchanged"


def test_instance_change_between_requests_starts_a_new_sequence():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), "boot-1"), (True, ("m",), "boot-2"), (True, ("m",), "boot-2")])
    _lifecycle, _first, warm = _sequence(module, endpoint, cold=False, instance=True)
    assert warm["sequence_label"] == "lifecycle_boundary_observed_new_sequence"
    assert warm["endpoint_process_reused"] is False
    assert "endpoint instance identity changed" in warm["boundary_observations"]


def test_model_switch_or_dead_process_is_a_boundary_not_warm():
    module = _module()
    switched = _FakeEndpoint([(True, ("m",), None), (True, ("other",), None), (True, ("other",), None)])
    _l, _f, warm = _sequence(module, switched, cold=True, process=_Process())
    assert warm["sequence_label"] == "lifecycle_boundary_observed_new_sequence"
    assert "served model set changed" in warm["boundary_observations"]

    crashed = _FakeEndpoint([(True, ("m",), None)] * 3)
    _l, _f, warm = _sequence(module, crashed, cold=True, process=_Process(rc=1))
    assert warm["sequence_label"] == "lifecycle_boundary_observed_new_sequence"


def test_boundary_after_warm_request_still_invalidates_the_warm_row():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None), (True, ("m",), None), (False, ("m",), None)])
    _l, _f, warm = _sequence(module, endpoint, cold=True, process=_Process())
    assert warm["sequence_label"] == "lifecycle_boundary_observed_new_sequence"
    assert any(item.startswith("after warm request:") for item in warm["boundary_observations"])


def test_launcher_exit_zero_does_not_prove_reuse():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3)
    _l, _f, warm = _sequence(module, endpoint, cold=True, process=_Process(rc=0))
    assert warm["sequence_label"] == "subsequent_request_endpoint_reuse_unproven"
    assert warm["harness_process_state"] == "launcher_exited"


def test_warm_prompt_does_not_share_a_cacheable_prefix_with_first():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3)
    _sequence(module, endpoint, cold=True, process=_Process())
    first, second = endpoint.prompts
    assert first[0] != second[0], "prompts must diverge at the first character"
    assert first.split(" perf: ", 1)[1] == second.split(" perf: ", 1)[1] == "p"


def test_backend_memory_is_reported_only_from_an_explicit_hook():
    module = _module()
    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3, memory=123)
    _l, first, warm = _sequence(module, endpoint, cold=True, process=_Process(), memory=True)
    assert first["backend_memory"] == {"supported": True, "backend_reported_memory": 123, "memory_source": "backend_reported"}
    assert warm["backend_memory"]["supported"] is True

    endpoint = _FakeEndpoint([(True, ("m",), None)] * 3)
    _l, first, _w = _sequence(module, endpoint, cold=True, process=_Process())
    assert first["backend_memory"]["supported"] is False
