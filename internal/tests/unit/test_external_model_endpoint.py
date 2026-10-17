"""Pointing a session at someone else's model endpoint is an experiment, not surgery.

An explicit --base-url, or a non-local preset such as ``cloud``, names an endpoint LCA
does not own: the session must not start, replace or stop a server for it, must say so
in the header, must observe the served model with the configured key, and must refuse
before any effect when no endpoint is configured at all.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import threading

import pytest

from local_agent.session.runtime_facts import models_fetcher, observed_model_id

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "session-hub.py"


@contextmanager
def _models_endpoint(*, required_key: str | None = None, model: str = "strong-model-1"):
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            seen.append(self.headers.get("Authorization"))
            if required_key is not None and self.headers.get("Authorization") != f"Bearer {required_key}":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"data": [{"id": model}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_keyed_endpoint_is_observed_with_its_key_and_a_local_one_without():
    with _models_endpoint(required_key="sk-test") as (url, seen):
        assert observed_model_id(url, fetch=models_fetcher("sk-test")) == "strong-model-1"
        assert seen == ["Bearer sk-test"]
    with _models_endpoint() as (url, seen):
        for unset in ("unused", "unset", ""):
            assert observed_model_id(url, fetch=models_fetcher(unset)) == "strong-model-1"
        assert seen == [None, None, None]


def _load_hub():
    spec = spec_from_file_location("session_hub_external_endpoint", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launch(hub, tmp_path, monkeypatch, argv):
    observed: dict[str, object] = {}

    def ensure_runtime(profile, config, runtime_root):
        observed["ensured"] = profile
        return hub.RuntimeEnsureResult(hub.RuntimeLifecycle.REUSED, "ready")

    class FakeApp:
        def run(self):
            observed["app_ran"] = True

    @contextmanager
    def fake_textual(service, opened, gateway, *, runtime_summary=None, stop_task=None):
        observed["runtime_summary"] = runtime_summary
        yield type("TextualRuntime", (), {"app": FakeApp()})()

    monkeypatch.setattr(hub, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(hub, "ensure_managed_runtime", ensure_runtime)
    monkeypatch.setattr(hub, "build_textual_session_runtime", fake_textual)
    observed["code"] = hub.main(argv)
    return observed


def test_base_url_opens_on_an_unmanaged_endpoint_and_says_so(tmp_path, monkeypatch):
    hub = _load_hub()
    with _models_endpoint(model="served-elsewhere") as (url, _seen):
        observed = _launch(hub, tmp_path, monkeypatch,
                           ["--repo", str(REPO), "--profile", "ptl-npu-8b",
                            "--base-url", url, "--model", "served-elsewhere"])
    assert observed["code"] == 0 and observed["app_ran"] is True
    assert "ensured" not in observed, "LCA tried to manage a server it does not own"
    summary = str(observed["runtime_summary"])
    assert hub.EXTERNAL_ENDPOINT_SUMMARY in summary
    assert "model served-elsewhere (observed)" in summary
    assert f"endpoint {url}" in summary
    assert "server reused" not in summary


def test_a_local_preset_at_its_own_endpoint_is_still_managed(tmp_path, monkeypatch):
    hub = _load_hub()
    monkeypatch.setattr(hub.RuntimeFacts, "observe", staticmethod(
        lambda preset, config, *, execution_enabled, fetch=None: hub.RuntimeFacts(
            preset=preset, endpoint=config.base_url, declared_model=config.model,
            declared_device=config.device, observed_model=None,
            execution_enabled=execution_enabled)))
    observed = _launch(hub, tmp_path, monkeypatch, ["--repo", str(REPO), "--profile", "ptl-npu-8b"])
    assert observed["ensured"] == "ptl-npu-8b"
    assert hub.EXTERNAL_ENDPOINT_SUMMARY not in str(observed["runtime_summary"])


def test_the_cloud_preset_without_a_configured_endpoint_refuses_before_any_effect(
        tmp_path, monkeypatch, capsys):
    hub = _load_hub()
    monkeypatch.setitem(hub.MODEL_PRESETS, "cloud",
                        replace(hub.MODEL_PRESETS["cloud"], base_url=""))
    observed = _launch(hub, tmp_path, monkeypatch, ["--repo", str(REPO), "--profile", "cloud"])
    assert observed["code"] == 2
    assert "ensured" not in observed and "app_ran" not in observed
    assert "LOCAL_AGENT_CLOUD_BASE_URL" in capsys.readouterr().err
    assert not any(tmp_path.rglob("*.db")), "a session store was created for a refused open"


@pytest.mark.parametrize("profile", ["cloud"])
def test_the_cloud_preset_with_an_endpoint_is_unmanaged(tmp_path, monkeypatch, profile):
    hub = _load_hub()
    with _models_endpoint(required_key="sk-cloud", model="cloud-model") as (url, seen):
        monkeypatch.setitem(hub.MODEL_PRESETS, "cloud", replace(
            hub.MODEL_PRESETS["cloud"], base_url=url, model="cloud-model", api_key="sk-cloud"))
        observed = _launch(hub, tmp_path, monkeypatch, ["--repo", str(REPO), "--profile", profile])
    assert observed["code"] == 0 and "ensured" not in observed
    summary = str(observed["runtime_summary"])
    assert "model cloud-model (observed)" in summary
    assert hub.EXTERNAL_ENDPOINT_SUMMARY in summary
    assert seen and all(value == "Bearer sk-cloud" for value in seen)
    assert "sk-cloud" not in summary
