from dataclasses import replace
from pathlib import Path
import socket

import pytest
from local_agent.config import MODEL_PRESETS
from serving import serve


def test_pull_prepares_namespace_and_posix_path_before_ovms_clone(tmp_path, monkeypatch):
    config = MODEL_PRESETS["nuc-cpu-8b"]
    plan = serve.make_plan("nuc-cpu-8b", config, tmp_path)
    repository = Path(plan["model_repository"])
    destination = repository / config.model
    monkeypatch.setattr(serve, "check_disk", lambda *args: None)
    calls = []

    def clone(argv, *, check, env):
        assert destination.parent.is_dir(), "OVMS requires the namespace before clone"
        assert not destination.exists(), "an empty target would make OVMS skip downloading"
        assert check is True
        calls.append(argv)

    monkeypatch.setattr(serve.subprocess, "run", clone)
    serve.pull(plan, config)
    assert len(calls) == 1
    assert calls[0][calls[0].index("--source_model") + 1] == config.model
    assert calls[0][calls[0].index("--model_repository_path") + 1] == repository.as_posix()


@pytest.mark.parametrize("model", ["../outside/model", ".", "nested/../../outside"])
def test_pull_refuses_escaping_or_empty_destination_before_effects(tmp_path, monkeypatch, model):
    config = replace(MODEL_PRESETS["nuc-cpu-8b"], model=model)
    plan = serve.make_plan("nuc-cpu-8b", config, tmp_path)
    monkeypatch.setattr(serve.subprocess, "run", lambda *a, **k: pytest.fail("download started"))
    with pytest.raises(serve.Refusal, match="within the model repository"):
        serve.pull(plan, config)
    assert not Path(plan["model_repository"]).exists()
    assert not Path(plan["state_file"]).parent.exists()


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


def test_local_runtimes_come_from_the_model_choice_owner():
    from serving import model_choice
    assert serve.LOCAL_RUNTIMES is model_choice.LOCAL_RUNTIMES
    assert serve.local_presets is model_choice.local_presets
    with pytest.raises(serve.Refusal, match="not a local serving runtime"):
        serve.validate(replace(MODEL_PRESETS["ptl-npu-8b"], runtime="cloud"))
    for name in model_choice.local_presets():
        serve.validate(MODEL_PRESETS[name])


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
