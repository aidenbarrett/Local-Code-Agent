from __future__ import annotations

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "session-hub.py"


def _load_hub():
    spec = spec_from_file_location("session_hub_public_textual_contract", SCRIPT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_session_launches_textual_with_real_durable_object_graph(tmp_path, monkeypatch):
    hub = _load_hub()
    observed: dict[str, object] = {}
    # Construct through the composition root's imported class. This journey exercises
    # a dynamically loaded product root, so importing a second test-side class identity
    # would test Python module-loading trivia rather than the Session Hub contract.
    facts = hub.RuntimeFacts(
        preset="ptl-npu-8b",
        endpoint="http://127.0.0.1:9999/v1",
        declared_model="configured-model",
        declared_device="NPU",
        observed_model="served-model",
        execution_enabled=False,
    )

    class Ensured:
        ok = True
        message = "ready"

    def ensure_runtime(profile, config, runtime_root):
        observed["ensure_profile"] = profile
        observed["ensure_config"] = config
        observed["ensure_root"] = runtime_root
        return Ensured()

    def observe_runtime(preset, config, *, execution_enabled, fetch=None):
        observed["facts_preset"] = preset
        observed["facts_execution"] = execution_enabled
        return facts

    class FakeApp:
        def run(self):
            observed["app_ran"] = True

    @contextmanager
    def fake_textual(service, opened, gateway, *, runtime_summary=None, stop_task=None):
        observed["service"] = service
        observed["opened"] = opened
        observed["gateway"] = gateway
        observed["runtime_summary"] = runtime_summary
        observed["stop_task"] = stop_task
        yield type("TextualRuntime", (), {"app": FakeApp()})()

    monkeypatch.setattr(hub, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(hub, "ensure_managed_runtime", ensure_runtime)
    monkeypatch.setattr(hub.RuntimeFacts, "observe", staticmethod(observe_runtime))
    monkeypatch.setattr(hub, "build_textual_session_runtime", fake_textual)

    result = hub.main(["--repo", str(REPO), "--profile", "ptl-npu-8b"])

    assert result == 0
    assert observed["app_ran"] is True
    assert observed["ensure_profile"] == "ptl-npu-8b"
    assert observed["ensure_root"] == tmp_path
    assert observed["facts_preset"] == "ptl-npu-8b"
    assert observed["facts_execution"] is False
    assert observed["runtime_summary"] == facts.header()
    assert callable(observed["stop_task"])

    gateway = observed["gateway"]
    assert isinstance(gateway, hub.RuntimeFactsGateway)
    assert isinstance(gateway.task_runner, hub.DurableTaskAdmissionRunner)
    executor = gateway.task_runner.executor
    assert isinstance(executor, hub.CancellableDurableTaskExecutor)
    assert isinstance(executor.controller, hub.AdmittedDurableTaskController)
    assert executor.controller.service is observed["service"]
    assert gateway.runtime_facts is facts
    # Source-changing work has an isolated place to happen, outside the user's checkout.
    workspaces = executor.controller.controller.workspaces
    assert isinstance(workspaces, hub.GitWorkspaceManager)
    assert workspaces.workspaces_root == (tmp_path / "ws").resolve()
