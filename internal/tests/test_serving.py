from dataclasses import replace
from pathlib import Path
import socket

import pytest
from local_agent.config import MODEL_PRESETS
from serving import serve


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_plan_uses_explicit_model_device_and_port(tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    plan = serve.make_plan("test", config, tmp_path, windows=True)
    assert plan["args"][plan["args"].index("--target_device") + 1] == config.device
    assert plan["args"][plan["args"].index("--rest_port") + 1] == str(plan["port"])
    assert plan["model_configuration"]["model"] == config.model


def test_context_budget_must_fit_server_envelope(tmp_path):
    config = MODEL_PRESETS["ptl-npu-8b"]
    with pytest.raises(serve.Refusal, match="context budget"):
        serve.make_plan(
            "bad",
            replace(config, context_budget_tokens=config.server_max_prompt_length),
            tmp_path,
        )


def test_status_does_not_adopt_foreign_endpoint(tmp_path, monkeypatch):
    config = replace(
        MODEL_PRESETS["nuc-llama-8b"],
        base_url=f"http://127.0.0.1:{free_port()}/v1",
    )
    plan = serve.make_plan("state", config, tmp_path)
    monkeypatch.setattr(serve, "get_json", lambda url: (200, {"data": [{"id": config.model}]}))
    assert serve.status(plan)["healthy"] is False


def test_pid_identity_mismatch_refuses_stop(tmp_path, monkeypatch):
    config = MODEL_PRESETS["nuc-llama-8b"]
    plan = serve.make_plan("reuse", config, tmp_path)
    record = {"pid": 1, "create_time": 1.0, "process_exe": "python", "plan": plan}
    serve.write_json(plan["state_file"], record)

    class WrongProcess:
        def is_running(self): return True
        def status(self): return "running"
        def create_time(self): return 2.0

    import psutil
    monkeypatch.setattr(psutil, "Process", lambda pid: WrongProcess())
    with pytest.raises(serve.Refusal, match="another process"):
        serve.stop(plan)
    assert Path(plan["state_file"]).exists()
