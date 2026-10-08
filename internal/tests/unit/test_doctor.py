"""`local-code-agent.ps1 doctor`: read-only readiness with one next action per gap (#458)."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

from local_agent.config import DEFAULT_MODEL_PRESET, MODEL_PRESETS
from serving import serve

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "doctor.py"
PYPROJECT = REPO / "pyproject.toml"


def _doctor():
    spec = importlib.util.spec_from_file_location("doctor_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor_under_test"] = module
    spec.loader.exec_module(module)
    return module


doctor = _doctor()


def _repo(root: Path, body: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".local-agent.toml").write_text(body if body is not None else (
        '[repo]\nname = "demo"\n'
        '[profiles.dev]\nconfigure = []\nbuild = ["cmake", "--build", "build"]\n'
        'test = ["ctest"]\n[policy]\nallow_patch = true\n'
    ), encoding="utf-8")
    return root


def _tools(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


_ALL_TOOLS = _tools("git", "cmake", "c++", "ovms", "ovms.exe", "llama-server", "llama-server.exe")


def _not_running(_plan):
    return {"healthy": False, "process_alive": False, "pid": None,
            "models_http_status": None, "health_http_status": None}


def _assess(tmp_path: Path, *, repo: Path | None = None, runtime_root: Path | None = None,
            **observed):
    seen = {
        "env": {},
        "which": _ALL_TOOLS,
        "endpoint_status": _not_running,
        "windows": False,
        "launcher": doctor.LauncherFacts(),
    }
    seen.update(observed)
    checks = doctor.assess(
        repo or _repo(tmp_path / "repo"),
        runtime_root or tmp_path / "runtime",
        pyproject=PYPROJECT,
        observers=doctor.Observers(**seen),
    )
    return {check.name: check for check in checks}


def _complete_weights(runtime_root: Path, profile: str) -> None:
    folder = runtime_root / "models" / MODEL_PRESETS[profile].model
    folder.mkdir(parents=True)
    for name in ("openvino_model.xml", "openvino_model.bin", "openvino_tokenizer.xml",
                 "openvino_tokenizer.bin", "openvino_detokenizer.xml",
                 "openvino_detokenizer.bin", "config.json"):
        (folder / name).write_text("x", encoding="utf-8")


@pytest.fixture(autouse=True)
def _python_ready(monkeypatch):
    """The Python row has its own test; keep the others independent of this interpreter."""
    monkeypatch.setattr(doctor, "check_python",
                        lambda _pyproject: doctor.Check("Python", doctor.READY, observed="test"))


def test_a_fresh_machine_names_the_weights_download_and_writes_nothing(tmp_path):
    runtime_root = tmp_path / "runtime"
    checks = _assess(tmp_path, runtime_root=runtime_root)
    assert checks["Model profile"].state == doctor.READY
    assert DEFAULT_MODEL_PRESET in (checks["Model profile"].configured or "")
    weights = checks["Weights"]
    assert weights.state == doctor.MISSING
    assert weights.next_action == serve.pull_command(DEFAULT_MODEL_PRESET)
    assert checks["Endpoint"].state == doctor.INFO
    assert not runtime_root.exists(), "check mode created the runtime root"


def test_complete_weights_are_ready(tmp_path):
    runtime_root = tmp_path / "runtime"
    _complete_weights(runtime_root, DEFAULT_MODEL_PRESET)
    checks = _assess(tmp_path, runtime_root=runtime_root)
    assert checks["Weights"].state == doctor.READY
    assert checks["Weights"].next_action is None


@pytest.mark.parametrize("windows", [False, True])
def test_a_missing_compiler_names_setup(tmp_path, windows):
    checks = _assess(tmp_path, windows=windows, which=_tools("git", "cmake", "ovms"),
                     launcher=doctor.LauncherFacts(probed=True, msvc_installation=None),
                     env={"LCA_OVMS_EXECUTABLE": "C:/ovms/ovms.exe"})
    compiler = checks["C++ compiler"]
    assert compiler.state == doctor.MISSING
    assert compiler.next_action == r".\install.ps1"


def test_the_launchers_msvc_fact_is_reported_as_observed(tmp_path):
    checks = _assess(tmp_path, windows=True,
                     launcher=doctor.LauncherFacts(probed=True, msvc_installation="C:/VS/BuildTools"),
                     env={"LCA_OVMS_EXECUTABLE": "C:/ovms/ovms.exe"})
    assert checks["C++ compiler"].state == doctor.READY
    assert "C:/VS/BuildTools" in (checks["C++ compiler"].observed or "")
    assert checks["Model server"].state == doctor.READY


def test_windows_facts_stay_unknown_without_the_launcher(tmp_path):
    checks = _assess(tmp_path, windows=True)
    assert checks["C++ compiler"].state == doctor.UNKNOWN
    assert checks["Model server"].state == doctor.UNKNOWN
    assert checks["C++ compiler"].next_action is None


def test_missing_git_and_cmake_name_setup(tmp_path):
    checks = _assess(tmp_path, which=_tools("c++", "ovms"))
    for name in ("Git", "CMake"):
        assert checks[name].state == doctor.MISSING
        assert checks[name].next_action == r".\install.ps1"


def test_missing_managed_ovms_names_setup_before_the_weights(tmp_path):
    checks = _assess(tmp_path, windows=True, launcher=doctor.LauncherFacts(probed=True))
    server = checks["Model server"]
    assert server.state == doctor.MISSING and server.next_action == r".\install.ps1"
    names = list(checks)
    assert names.index("Model server") < names.index("Weights")


@pytest.mark.parametrize("stored", ["{not json", json.dumps({"preset": "no-such-preset"})])
def test_a_corrupt_or_unknown_model_choice_is_blocked_never_defaulted(tmp_path, stored):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "model-preset.json").write_text(stored, encoding="utf-8")
    checks = _assess(tmp_path, runtime_root=runtime_root)
    profile = checks["Model profile"]
    assert profile.state == doctor.BLOCKED
    assert profile.next_action is not None and "models use" in profile.next_action
    for dependent in ("Model server", "Weights", "Endpoint"):
        assert checks[dependent].state == doctor.UNKNOWN
    assert (runtime_root / "model-preset.json").read_text(encoding="utf-8") == stored


def test_an_offline_endpoint_is_information_not_a_failure(tmp_path):
    endpoint = _assess(tmp_path)["Endpoint"]
    assert endpoint.state == doctor.INFO
    assert endpoint.configured is not None and "127.0.0.1" in endpoint.configured
    assert endpoint.observed is not None and "not running" in endpoint.observed


def test_a_foreign_server_on_the_port_is_blocked_with_one_action(tmp_path):
    def foreign(_plan):
        return {"healthy": False, "process_alive": False, "pid": None,
                "models_http_status": 200, "health_http_status": 200}

    endpoint = _assess(tmp_path, endpoint_status=foreign)["Endpoint"]
    assert endpoint.state == doctor.BLOCKED
    assert endpoint.next_action is not None and endpoint.next_action.startswith("stop the server")


def test_an_owned_healthy_endpoint_is_ready(tmp_path):
    def owned(_plan):
        return {"healthy": True, "process_alive": True, "pid": 42,
                "models_http_status": 200, "health_http_status": 200}

    assert _assess(tmp_path, endpoint_status=owned)["Endpoint"].state == doctor.READY


def test_unprovable_endpoint_ownership_stays_unknown(tmp_path):
    def refused(_plan):
        raise serve.Refusal("PID now belongs to another process; refusing to adopt or stop it")

    endpoint = _assess(tmp_path, endpoint_status=refused)["Endpoint"]
    assert endpoint.state == doctor.UNKNOWN
    assert "another process" in (endpoint.observed or "")


def test_the_real_endpoint_owner_observes_a_closed_port_without_starting_anything(
        tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    config = MODEL_PRESETS[DEFAULT_MODEL_PRESET]
    monkeypatch.setitem(MODEL_PRESETS, DEFAULT_MODEL_PRESET,
                        dataclasses.replace(config, base_url=f"http://127.0.0.1:{free_port}/v3"))
    for effect in ("launch", "stop", "pull", "preflight"):
        monkeypatch.setattr(serve, effect, _forbidden(effect))
    endpoint = _assess(tmp_path, endpoint_status=serve.status)["Endpoint"]
    assert endpoint.state == doctor.INFO
    assert f":{free_port}" in (endpoint.configured or "")


def _forbidden(name):
    def refuse(*_args, **_kwargs):
        raise AssertionError(f"doctor called serve.{name}")
    return refuse


def test_repository_states(tmp_path):
    (tmp_path / "plain").mkdir()
    missing = _assess(tmp_path, repo=tmp_path / "plain")["Repository"]
    assert missing.state == doctor.MISSING
    assert missing.next_action is not None and ".local-agent.toml" in missing.next_action

    broken = _repo(tmp_path / "broken", "[profiles\n")
    blocked = _assess(tmp_path, repo=broken)["Repository"]
    assert blocked.state == doctor.BLOCKED and blocked.next_action is not None

    ready = _assess(tmp_path)["Repository"]
    assert ready.state == doctor.READY
    assert ready.configured is not None
    assert "policy allows build, test, patch" in ready.configured


def test_python_row_reports_the_runtime_contract(tmp_path, monkeypatch):
    monkeypatch.undo()
    contract = tmp_path / "pyproject.toml"
    contract.write_text('[project]\nname = "x"\ndependencies = ["no-such-distribution-lca"]\n',
                        encoding="utf-8")
    python = doctor.check_python(contract)
    assert python.state == doctor.BLOCKED
    assert python.next_action == r".\install.ps1"
    assert "no-such-distribution-lca" in (python.observed or "")


def test_a_next_action_exists_exactly_when_a_row_is_not_ready():
    with pytest.raises(ValueError):
        doctor.Check("Weights", doctor.MISSING)
    with pytest.raises(ValueError):
        doctor.Check("Weights", doctor.READY, next_action="anything")


def test_main_exits_3_when_not_ready_and_repeats_identically(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(doctor.shutil, "which", _ALL_TOOLS)
    monkeypatch.setattr(doctor.serve, "status", _not_running)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    argv = ["--repo", str(repo), "--runtime-root", str(runtime_root)]
    assert doctor.main(argv) == doctor.EXIT_NOT_READY
    first = capsys.readouterr().out
    assert doctor.main(argv) == doctor.EXIT_NOT_READY
    assert capsys.readouterr().out == first
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert "Weights        MISSING" in first
    assert serve.pull_command(DEFAULT_MODEL_PRESET) in first


def test_main_exits_0_when_everything_is_ready(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    runtime_root = tmp_path / "runtime"
    _complete_weights(runtime_root, DEFAULT_MODEL_PRESET)
    monkeypatch.setattr(doctor.shutil, "which", _ALL_TOOLS)
    monkeypatch.setattr(doctor.serve, "status", _not_running)
    assert doctor.main(["--repo", str(repo), "--runtime-root", str(runtime_root)]) == 0
    assert "Ready. Open the Session Hub" in capsys.readouterr().out


def test_help_names_doctor():
    help_text = (REPO / "internal" / "scripts" / "product-help.py").read_text(encoding="utf-8")
    assert 'term.field("doctor"' in help_text


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")


@pytest.mark.skipif(os.name != "nt", reason="the public launcher is Windows PowerShell")
def test_the_public_launcher_runs_doctor_read_only_twice(tmp_path):
    powershell = _powershell()
    assert powershell, "a Windows CI runner must provide PowerShell"
    repo = _repo(tmp_path / "repo")
    runtime_root = tmp_path / "runtime"
    env = {**os.environ, "LCA_RUNTIME_ROOT": str(runtime_root)}
    outputs = []
    for _launch in range(2):
        result = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(REPO / "local-code-agent.ps1"), "doctor", "--repo", str(repo)],
            capture_output=True, text=True, timeout=180, env=env, cwd=repo,
        )
        assert result.returncode == doctor.EXIT_NOT_READY, result.stdout + result.stderr
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert "Weights        MISSING" in outputs[0]
    assert "C++ compiler   UNKNOWN" not in outputs[0], "the launcher did not supply its MSVC fact"
    assert "Model server   UNKNOWN" not in outputs[0], "the launcher did not supply its OVMS fact"
    assert not runtime_root.exists(), "doctor created the runtime root"
