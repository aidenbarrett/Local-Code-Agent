"""The native C++ endpoint must satisfy the product's own clients.

These tests launch a built ``lca-endpoint`` with its deterministic fixture
backend and drive it with the code the product actually uses: the
OpenAI-compatible client, the serving qualification check and the endpoint
performance harness. Nothing here is a model-quality claim; the fixture is
not a model.

The binary comes from ``LCA_NATIVE_ENDPOINT_BIN``. Without it the module is
skipped, except when ``LCA_REQUIRE_NATIVE_ENDPOINT=1`` (set by the
native-endpoint CI workflow), where a missing binary is a failure.
"""
from __future__ import annotations

from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest

from local_agent.config import MODEL_PRESETS
from local_agent.llm.client import OpenAICompatibleClient
from serving import qualify_server

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_CONFIG = ROOT / "internal" / "native_endpoint" / "tests" / "fixtures" / "conformance_fixture.json"
HARNESS = ROOT / "internal" / "perf" / "endpoint_harness.py"
MODEL_ID = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))["model_id"]
# A product profile whose tool-call format (hermes) and thinking setting match
# what the endpoint renders; only base_url and model are overridden.
PROFILE = "ptl-npu-8b"


def _binary() -> Path:
    value = os.environ.get("LCA_NATIVE_ENDPOINT_BIN", "")
    required = os.environ.get("LCA_REQUIRE_NATIVE_ENDPOINT") == "1"
    if not value:
        if required:
            pytest.fail("LCA_REQUIRE_NATIVE_ENDPOINT=1 but LCA_NATIVE_ENDPOINT_BIN is not set")
        pytest.skip("LCA_NATIVE_ENDPOINT_BIN is not set")
    path = Path(value)
    if not path.is_file():
        pytest.fail(f"LCA_NATIVE_ENDPOINT_BIN does not name a file: {path}")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str):
    with urlopen(url, timeout=10) as response:  # noqa: S310 - local test endpoint
        return json.loads(response.read().decode("utf-8"))


@pytest.fixture(scope="module")
def endpoint():
    binary = _binary()
    port = _free_port()
    process = subprocess.Popen(
        [str(binary), "--backend", "fixture", "--backend-config", str(FIXTURE_CONFIG),
         "--port", str(port), "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                _get_json(base + "/v1/models")
                break
            except (HTTPError, URLError, ConnectionError):
                if process.poll() is not None:
                    raise AssertionError(f"endpoint exited early: {process.stderr.read().decode(errors='replace')}")
                if time.monotonic() > deadline:
                    raise AssertionError("endpoint did not become ready")
                time.sleep(0.05)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    # A clean shutdown on terminate is part of the contract (the harness and
    # the serving controller both stop endpoints this way). Windows has no
    # SIGTERM; terminate() there is TerminateProcess and the code is 1.
    if os.name != "nt":
        assert process.returncode == 0, process.stderr.read().decode(errors="replace")


def _config(base: str):
    return replace(MODEL_PRESETS[PROFILE], base_url=base + "/v1/", model=MODEL_ID)


def _harness():
    spec = spec_from_file_location("endpoint_harness_for_native_endpoint", HARNESS)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_serving_qualification_passes(endpoint):
    report = qualify_server.run(PROFILE, endpoint + "/v1/", MODEL_ID, [1000, 4000, 7000])
    failed = [(c.name, c.detail) for c in report.checks if c.status != qualify_server.PASS]
    assert failed == []
    names = {c.name for c in report.checks}
    assert {"configured model present", "plain chat", "schema constrained tool call"} <= names


def test_product_client_streams_with_server_provenance(endpoint):
    client = OpenAICompatibleClient(_config(endpoint))
    reply = client.chat([{"role": "user", "content": "Reply with exactly READY"}], tools=None, max_tokens=16)
    assert reply.content.strip() == "READY"
    assert reply.stats.streamed is True
    assert reply.stats.prompt_tokens > 0
    assert reply.stats.completion_tokens > 0
    # Backend-reported timings reach the client as server-reported fields.
    assert reply.stats.server_prompt_ms is not None
    assert reply.stats.system_fingerprint.startswith("lca-endpoint-")


def test_product_client_unary_tool_call(endpoint):
    client = OpenAICompatibleClient(_config(endpoint)).as_unary()
    tool = {"type": "function", "function": {
        "name": "echo_value", "description": "Return one supplied string.",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}
    reply = client.chat(
        [{"role": "user", "content": "Call echo_value with value READY. Do not answer directly."}],
        tools=[tool], max_tokens=128)
    assert reply.finish_reason == "tool_calls"
    assert [(call.name, call.arguments) for call in reply.tool_calls] == [("echo_value", {"value": "READY"})]


def test_oversized_prompt_is_rejected_not_truncated(endpoint):
    client = OpenAICompatibleClient(_config(endpoint))
    with pytest.raises(Exception) as caught:
        client.chat([{"role": "user", "content": "x " * 9000}], tools=None, max_tokens=1)
    assert "context_length_exceeded" in str(caught.value)


def test_harness_proves_identity_and_cancellation(endpoint):
    harness = _harness()
    profile = harness.Profile(
        name="native-endpoint-fixture",
        base_url=endpoint + "/v1/",
        model=MODEL_ID,
        runtime="lca-fixture",
        device="none",
        identity_url=endpoint + "/identity",
        identity_model_path="model.id",
        identity_runtime_path="runtime.name",
        identity_device_path="device.requested",
        telemetry_url=endpoint + "/telemetry",
        prefill_time_path="last_request.backend_prefill_ms",
        active_requests_url=endpoint + "/debug/active-requests",
        active_request_ids_path="active_request_ids",
        request_id_header="x-request-id",
    )
    client = harness.EndpointClient(profile)

    identity = harness.capture_identity(client, profile)
    assert identity["model_verified_by_models_endpoint"] is True
    assert identity["model_matches_observed"] is True
    assert identity["runtime_matches_observed"] is True

    ladder = harness.benchmark_context_ladder(client, profile, [1000, 20000])
    assert ladder[0]["supported"] is True
    assert ladder[0]["prefill_time_source"] == "backend_reported"
    assert ladder[1]["supported"] is False

    cancellation = harness.benchmark_cancellation(client, profile)
    assert cancellation["supported"] is True, cancellation
    assert cancellation["timing_source"] == "endpoint_observation"
    assert cancellation["disconnect_to_endpoint_stop_ms"] < 5000
    assert _get_json(endpoint + "/debug/active-requests")["active_request_ids"] == []
