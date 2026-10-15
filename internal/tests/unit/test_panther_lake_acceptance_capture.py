from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "panther-lake-acceptance.py"


def _load():
    spec = importlib.util.spec_from_file_location("panther_lake_acceptance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_gate_refuses_any_active_default_route():
    module = _load()
    with pytest.raises(module.AcceptanceCaptureError, match="active default route"):
        module.require_offline(
            [{"DestinationPrefix": "0.0.0.0/0", "InterfaceAlias": "Wi-Fi"}]
        )


def test_offline_gate_accepts_no_default_routes():
    module = _load()
    module.require_offline([])


@pytest.mark.parametrize("result", [None, {}, "", [None]])
def test_route_probe_refuses_unknown_instead_of_reporting_offline(monkeypatch, result):
    module = _load()
    monkeypatch.setattr(module, "_powershell_json", lambda _script: result)
    with pytest.raises(module.AcceptanceCaptureError, match="observed route list"):
        module.active_default_routes()


def _powershell_probe(module, monkeypatch, prelude):
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    def probe(script):
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", prelude + script],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise module.AcceptanceCaptureError(result.stderr)
        return json.loads(result.stdout)

    monkeypatch.setattr(module, "_powershell_json", probe)


def test_route_probe_serializes_an_observed_empty_list(monkeypatch):
    module = _load()
    _powershell_probe(module, monkeypatch, "function Get-NetRoute { param($ErrorAction) };\n")
    assert module.active_default_routes() == []


def test_route_adapter_observation_failure_never_becomes_offline(monkeypatch):
    module = _load()
    _powershell_probe(module, monkeypatch, r'''
function Get-NetRoute { param($ErrorAction)
    [pscustomobject]@{DestinationPrefix='0.0.0.0/0';State='Alive';InterfaceIndex=12}
}
function Get-NetAdapter { [CmdletBinding()] param($InterfaceIndex)
    Write-Error 'adapter lookup failed'
}
''')
    with pytest.raises(module.AcceptanceCaptureError, match="adapter lookup failed"):
        module.active_default_routes()


def test_device_driver_is_matched_to_the_observed_device(monkeypatch):
    module = _load()
    _powershell_probe(module, monkeypatch, r'''
function Get-PnpDevice { param([switch]$PresentOnly, $ErrorAction)
    [pscustomobject]@{FriendlyName='Intel GPU';Class='Display';Status='OK';InstanceId='gpu-id'}
}
function Get-CimInstance { param($ClassName, $ErrorAction)
    [pscustomobject]@{DeviceID='other-id';DriverVersion='wrong';DriverProviderName='other'}
    [pscustomobject]@{DeviceID='gpu-id';DriverVersion='observed';DriverProviderName='Intel'}
}
''')
    devices = module.configured_devices("GPU")
    assert len(devices) == 1
    assert devices[0]["instance_id"] == "gpu-id"
    assert devices[0]["driver_version"] == "observed"
    assert devices[0]["driver_provider"] == "Intel"


def test_npu_inventory_excludes_input_devices_and_preserves_named_accelerators(monkeypatch):
    module = _load()
    _powershell_probe(module, monkeypatch, r'''
function Get-PnpDevice { param([switch]$PresentOnly, $ErrorAction)
    foreach ($name in @('USB Input Device','Input Controller','QuantumNPU','Neural Network Adapter',
                        'Intel(R) NPU Accelerator','Intel(R) AI Boost','Neural Processing Unit', $null)) {
        [pscustomobject]@{FriendlyName=$name;Status='OK';InstanceId=$name}
    }
}
function Get-CimInstance { param($ClassName, $ErrorAction) }
''')
    devices = module.configured_devices("NPU")
    assert [device["name"] for device in devices] == [
        "Intel(R) NPU Accelerator", "Intel(R) AI Boost", "Neural Processing Unit",
    ]
    assert all(device["driver_version"] is None for device in devices)


def test_serving_driver_inventory_uses_the_same_npu_name_boundary(monkeypatch):
    module = _load()
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    run = subprocess.run

    def probe(args, **kwargs):
        prelude = r'''
function Get-CimInstance { param($ClassName)
    [pscustomobject]@{DeviceName='USB Input Device';DriverVersion='wrong'}
    [pscustomobject]@{DeviceName='Intel(R) AI Boost';DriverVersion='observed'}
}
'''
        return run([shell, "-NoProfile", "-Command", prelude + args[-1]], **kwargs)

    monkeypatch.setattr(module.serve, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.serve.subprocess, "run", probe)
    assert module.serve.driver_version() == {
        "drivers": {"DeviceName": "Intel(R) AI Boost", "DriverVersion": "observed"},
        "quality": "observed",
    }


def test_preflight_parser_requires_and_retains_selected_profile():
    module = _load()
    args = module.parser().parse_args(
        [
            "preflight",
            "--repo", ".",
            "--profile", "ptl-gpu-30b",
            "--runtime-root", "runtime",
            "--output", "preflight.json",
            "--isolation-method", "Wi-Fi disabled; Ethernet unplugged",
        ]
    )

    assert args.profile == "ptl-gpu-30b"
    assert args.isolation_method == "Wi-Fi disabled; Ethernet unplugged"


def _installed_artifacts(module, runtime_root):
    profile = "ptl-gpu-30b"
    config = module.MODEL_PRESETS[profile]
    plan = module.serve.make_plan(
        profile, config, runtime_root, executable=module.os.environ.get("LCA_OVMS_EXECUTABLE"),
    )
    launch = Path(plan["spec_file"])
    launch.parent.mkdir(parents=True)
    launch.write_text(json.dumps(plan), encoding="utf-8")
    qualification = runtime_root / "reports" / f"qualification-{profile}.json"
    qualification.parent.mkdir(parents=True)
    report = {"schema_version": 1, "profile": profile, "model": config.model,
              "base_url": config.base_url, "failed": False,
              "checks": [{"name": name, "status": "PASS", "detail": "observed", "data": {}}
                         for name in ("configured model present", "plain chat",
                                      "schema constrained tool call")]}
    qualification.write_text(json.dumps(report), encoding="utf-8")
    return launch, qualification


def test_preflight_uses_current_install_and_serving_artifacts(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local-code-agent.ps1").write_text("launcher", encoding="utf-8")
    runtime_root = tmp_path / "runtime-root"
    launch, qualification = _installed_artifacts(module, runtime_root)
    output = tmp_path / "run" / "preflight.json"
    output.parent.mkdir()
    (output.parent / "qualification-preflight.json").write_text(
        json.dumps(
            {
                "schema": module.PREPARE_SCHEMA,
                "phase": "prepare",
                "profile": "ptl-gpu-30b",
                "repository": {"head": "head"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr("builtins.input", lambda *_args: "Wi-Fi disabled")
    monkeypatch.setattr(module, "_git", lambda _repo, *args: "head" if "rev-parse" in args else "")
    monkeypatch.setattr(module, "active_default_routes", lambda: [])
    observed = {}

    def devices(device):
        observed["device"] = device
        return [{"name": "GPU device"}]

    monkeypatch.setattr(module, "configured_devices", devices)
    monkeypatch.setattr(module, "weight_state", lambda profile, root: "downloaded")

    def run(args, **_kwargs):
        observed["args"] = args
        observed["runtime_root"] = _kwargs["runtime_root"]
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(module, "_run", run)

    payload = module.capture_preflight(
        repo, runtime_root, output, "ptl-gpu-30b",
        "Wi-Fi disabled; Ethernet unplugged",
    )

    assert payload["profile"] == "ptl-gpu-30b"
    assert payload["offline_gate"]["operator_isolation_method"] == (
        "Wi-Fi disabled; Ethernet unplugged"
    )
    assert payload["runtime"]["device"] == "GPU"
    assert payload["qualification_summary"]["ok"] is True
    assert payload["artifacts"]["launch_plan"]["path"] == str(launch)
    assert payload["artifacts"]["qualification"]["path"] == str(qualification)
    assert observed["device"] == "GPU"
    assert payload["model_manifest_summary"] == {
        "source_model": module.MODEL_PRESETS["ptl-gpu-30b"].model,
        "weights": "downloaded",
    }
    assert observed["args"][-2:] == ["--profile", "ptl-gpu-30b"]
    assert observed["runtime_root"] == runtime_root


@pytest.mark.parametrize("artifact,field,value", [
    ("launch", "profile", "ptl-npu-8b"),
    ("launch", "args", []),
    ("qualification", "profile", "ptl-npu-8b"),
    ("qualification", "model", "wrong-model"),
    ("qualification", "base_url", "http://127.0.0.1:1/v3"),
    ("qualification", "schema_version", 2),
    ("qualification", "failed", True),
    ("qualification", "failed", 0),
    ("qualification", "checks", []),
    ("qualification", "checks", [{"name": [], "status": "PASS"}]),
])
def test_installed_evidence_refuses_stale_or_incomplete_artifacts(
    tmp_path, artifact, field, value,
):
    module = _load()
    launch, qualification = _installed_artifacts(module, tmp_path)
    path = launch if artifact == "launch" else qualification
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.AcceptanceCaptureError):
        module.installed_runtime_evidence("ptl-gpu-30b", tmp_path)


def test_preparation_checks_current_artifacts_before_ready(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "_git", lambda *_args: "head")
    monkeypatch.setattr(module, "weight_state", lambda *_args: "downloaded")
    monkeypatch.setattr(module, "_run", lambda *_args, **_kwargs:
                        subprocess.CompletedProcess([], 0, stdout="ready", stderr=""))
    runtime = tmp_path / "runtime"
    blocked = module.prepare_qualification(tmp_path, runtime, tmp_path / "missing", "ptl-gpu-30b")
    assert blocked["ready_to_disconnect"] is False
    assert "install.ps1" in blocked["blockers"][0]
    _installed_artifacts(module, runtime)
    ready = module.prepare_qualification(tmp_path, runtime, tmp_path / "ready", "ptl-gpu-30b")
    assert ready["ready_to_disconnect"] is True
    assert ready["offline_evidence"] == module.UNKNOWN


def test_custom_runtime_root_reaches_child_without_changing_parent(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setenv("LCA_RUNTIME_ROOT", "parent-root")
    result = module._run(
        [sys.executable, "-c", "import os; print(os.environ['LCA_RUNTIME_ROOT'])"],
        runtime_root=tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert module.os.environ["LCA_RUNTIME_ROOT"] == "parent-root"


@pytest.mark.parametrize("equals_form", [False, True])
def test_public_qualification_resolves_ovms_from_selected_root(tmp_path, equals_form):
    shell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    checkout = tmp_path / "checkout"
    scripts = checkout / "internal" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO / "local-code-agent.ps1", checkout / "local-code-agent.ps1")
    shutil.copy2(REPO / "internal" / "scripts" / "runtime-root.ps1", scripts)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    (scripts / "runtime-preflight.py").write_text("", encoding="utf-8")
    (scripts / "panther-lake-acceptance.py").write_text(
        "import os,json; print(json.dumps({k:os.environ.get(k) "
        "for k in ['LCA_RUNTIME_ROOT','LCA_OVMS_EXECUTABLE']}))", encoding="utf-8",
    )
    runtime = tmp_path / "custom runtime"
    ovms = runtime / "tools" / "ovms-2026.3.0"
    ovms.mkdir(parents=True)
    executable = ovms / "ovms.exe"
    executable.write_text("", encoding="utf-8")
    (ovms / "setupvars.ps1").write_text("# test setup\n", encoding="utf-8")
    env = {**_load().os.environ, "LCA_RUNTIME_ROOT": str(tmp_path / "default"),
           "LCA_OVMS_EXECUTABLE": str(tmp_path / "default" / "ovms.exe")}
    env["PATH"] = str(Path(sys.executable).parent) + _load().os.pathsep + env["PATH"]
    arguments = [f"--runtime-root={runtime}"] if equals_form else ["--runtime-root", str(runtime)]
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(checkout / "local-code-agent.ps1"), "qualify", *arguments],
        env=env, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip())
    assert Path(payload["LCA_RUNTIME_ROOT"]) == runtime
    assert Path(payload["LCA_OVMS_EXECUTABLE"]) == executable


@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize("relative_paths", [False, True])
def test_one_run_composes_existing_capture_and_acceptance_owners(
    tmp_path, monkeypatch, capsys, exit_code, relative_paths
):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local-code-agent.ps1").write_text("launcher", encoding="utf-8")
    output = tmp_path / "qualification"
    monkeypatch.chdir(tmp_path)
    input_repo = Path("repo") if relative_paths else repo
    input_runtime = Path("runtime") if relative_paths else tmp_path / "runtime"
    input_output = Path("qualification") if relative_paths else output
    observed = {}

    def prepare(_repo, _runtime, destination, profile):
        destination.mkdir()
        observed["prepared"] = profile
        return {"ready_to_disconnect": True, "blockers": []}

    def preflight(_repo, _runtime, destination, profile, isolation_method):
        payload = {
            "schema": module.SCHEMA,
            "phase": "preflight",
            "profile": profile,
            "repository": {"root": str(repo), "head": "abc", "clean": True},
            "offline_gate": {
                "passed": True,
                "operator_isolation_method": isolation_method,
            },
        }
        destination.write_text(json.dumps(payload), encoding="utf-8")
        observed["preflight"] = profile
        return payload

    def run(args, **_kwargs):
        destination = Path(args[args.index("--output") + 1])
        if not destination.is_absolute():
            destination = _kwargs["cwd"] / destination
        observed["runtime_root"] = _kwargs["runtime_root"]
        destination.mkdir(parents=True)
        rows = [
            {"id": journey_id, "status": "PASS", "reason": "", "measured": {}}
            for journey_id in ("J05-stop-build", "J07-stop-model", "J24-continuous")
        ]
        (destination / "journeys.json").write_text(
            json.dumps(
                {
                    "profile": "ptl-gpu-30b",
                    "preconditions": {"product": {"package_commit": "abc"}},
                    "journeys": rows,
                }
            ),
            encoding="utf-8",
        )
        observed["command"] = args
        return SimpleNamespace(returncode=exit_code, stdout="journey output", stderr="journey errors")

    monkeypatch.setattr(module, "prepare_qualification", prepare)
    monkeypatch.setattr(module, "capture_preflight", preflight)
    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "active_default_routes", lambda: [])
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr("builtins.input", lambda *_args: "Wi-Fi disabled")
    monkeypatch.setattr(
        module, "_git", lambda _repo, *args: "abc" if "rev-parse" in args else ""
    )

    if exit_code:
        with pytest.raises(module.AcceptanceCaptureError, match="reported failure"):
            module.run_qualification(
                input_repo, input_runtime, input_output, "ptl-gpu-30b",
                confirm_disconnected=True,
            )
        payload = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
    else:
        payload = module.run_qualification(
            input_repo, input_runtime, input_output, "ptl-gpu-30b",
            confirm_disconnected=True,
        )

    assert observed["prepared"] == observed["preflight"] == "ptl-gpu-30b"
    assert observed["command"][-3:] == ["--profile", "ptl-gpu-30b", "--allow-model"]
    assert observed["runtime_root"] == tmp_path / "runtime"
    assert not (repo / "qualification").exists(), "evidence was split into the checkout"
    assert payload["self_certified"] is False
    assert payload["offline_gate"]["operator_isolation_method"] == "Wi-Fi disabled"
    assert payload["run_evidence"]["journeys"]["J24-continuous"]["status"] == "PASS"
    assert (output / "acceptance.json").is_file()
    assert "Cold model ms   UNKNOWN" in (output / "summary.txt").read_text(encoding="utf-8")
    assert (output / "journeys.stdout.log").read_text(encoding="utf-8") == "journey output"
    assert (output / "journeys.stderr.log").read_text(encoding="utf-8") == "journey errors"
    visible = capsys.readouterr().out
    assert "READY TO DISCONNECT" in visible
    assert str(output / "journeys") in visible
    assert (output / "summary.txt").read_text(encoding="utf-8") in visible


def test_unready_run_prints_next_actions_and_never_crosses_disconnect_boundary(
    tmp_path, monkeypatch, capsys
):
    module = _load()
    monkeypatch.setattr(
        module, "prepare_qualification",
        lambda *_args: {"ready_to_disconnect": False, "blockers": ["install weights"]},
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("an unready run must never prompt or perform disconnected preflight")

    monkeypatch.setattr("builtins.input", forbidden)
    monkeypatch.setattr(module, "capture_preflight", forbidden)
    with pytest.raises(module.AcceptanceCaptureError, match="not ready to disconnect"):
        module.run_qualification(
            tmp_path, tmp_path, tmp_path / "output", "ptl-gpu-30b",
            confirm_disconnected=False,
        )
    assert capsys.readouterr().out == "NOT READY\nNEXT: install weights\n"


@pytest.mark.parametrize("response", ["", "yes", "disconnected"])
def test_run_requires_exact_disconnect_confirmation(tmp_path, monkeypatch, response):
    module = _load()
    monkeypatch.setattr(
        module, "prepare_qualification",
        lambda *_args: {"ready_to_disconnect": True, "blockers": []},
    )
    monkeypatch.setattr("builtins.input", lambda *_args: response)

    def forbidden(*_args, **_kwargs):
        pytest.fail("preflight cannot run without exact confirmation")

    monkeypatch.setattr(module, "capture_preflight", forbidden)
    with pytest.raises(module.AcceptanceCaptureError, match="exact confirmation") as raised:
        module.run_qualification(
            tmp_path, tmp_path, tmp_path / "output", "ptl-gpu-30b",
            confirm_disconnected=False,
        )
    assert "preparation evidence remains" in str(raised.value)
    assert "--output <new-output-folder>" in str(raised.value)


def test_ctrl_c_exits_cleanly_with_preparation_recovery_command(tmp_path, monkeypatch, capsys):
    module = _load()

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_qualification", interrupted)
    result = module.main([
        "run", "--repo", str(tmp_path), "--profile", "ptl-gpu-30b",
        "--output", str(tmp_path / "qualification"),
    ])

    assert result == 130
    error = capsys.readouterr().err
    assert "CANCELLED" in error
    assert "any completed preparation evidence was retained" in error
    from local_agent.public_commands import public_entrypoint
    assert f"{public_entrypoint()} qualify --profile ptl-gpu-30b" in error
    assert "--output <new-output-folder>" in error


@pytest.mark.parametrize(
    "method", ["", "   ", "Wi-Fi disabled\nEthernet unplugged", "Wi-Fi disabled\n"]
)
def test_run_refuses_missing_or_multiline_isolation_method(
    tmp_path, monkeypatch, method
):
    module = _load()
    monkeypatch.setattr(
        module, "prepare_qualification",
        lambda *_args: {"ready_to_disconnect": True, "blockers": []},
    )
    monkeypatch.setattr("builtins.input", lambda *_args: method)

    def forbidden(*_args, **_kwargs):
        pytest.fail("preflight cannot run without a retained isolation method")

    monkeypatch.setattr(module, "capture_preflight", forbidden)
    with pytest.raises(module.AcceptanceCaptureError, match="isolation method"):
        module.run_qualification(
            tmp_path, tmp_path, tmp_path / "output", "ptl-gpu-30b",
            confirm_disconnected=True,
        )


def test_composed_finalize_refuses_missing_required_journey(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "schema": module.SCHEMA,
                "phase": "preflight",
                "profile": "ptl-gpu-30b",
                "repository": {"root": str(repo), "head": "abc", "clean": True},
            }
        ),
        encoding="utf-8",
    )
    journeys = tmp_path / "journeys.json"
    journeys.write_text(
        json.dumps(
            {
                "profile": "ptl-gpu-30b",
                "preconditions": {"product": {"package_commit": "abc"}},
                "journeys": [
                    {"id": "J05-stop-build", "status": "PASS", "measured": {}}
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        module, "_git", lambda _repo, *args: "abc" if "rev-parse" in args else ""
    )

    with pytest.raises(module.AcceptanceCaptureError, match="missing required journeys"):
        module.finalize_qualification_run(
            preflight, journeys, tmp_path / "acceptance.json", "qualify"
        )


@pytest.mark.parametrize("defect,reason", [
    ("profile", "profile does not match"),
    ("source", "source identity does not match"),
    ("head", "checkout drifted"),
    ("dirty", "checkout became dirty"),
    ("schema", "schema/phase"),
    ("empty", "no journey outcomes"),
    ("duplicate", "duplicate journey"),
    ("duplicate-last", "duplicate journey"),
    ("duplicate-identical", "duplicate journey"),
    ("invalid-row", "invalid journey identity"),
    ("non-object-row", "invalid journey identity"),
])
def test_composed_finalize_refuses_unbound_or_stale_evidence(tmp_path, monkeypatch, defect, reason):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    prepared = {
        "schema": module.SCHEMA,
        "phase": "preflight",
        "profile": "ptl-gpu-30b",
        "repository": {"root": str(repo), "head": "abc", "clean": True},
    }
    report = {
        "profile": "ptl-gpu-30b",
        "preconditions": {"product": {"package_commit": "abc"}},
        "journeys": [
            {"id": jid, "status": "PASS"}
            for jid in ("J05-stop-build", "J07-stop-model", "J24-continuous")
        ],
    }
    if defect == "profile":
        report["profile"] = "ptl-npu-8b"
    elif defect == "source":
        report["preconditions"]["product"]["package_commit"] = "other"
    elif defect == "schema":
        prepared["schema"] = "obsolete"
    elif defect == "empty":
        report["journeys"] = []
    elif defect == "duplicate":
        report["journeys"].insert(0, {"id": "J07-stop-model", "status": "FAIL"})
    elif defect == "duplicate-last":
        report["journeys"].append({"id": "J07-stop-model", "status": "FAIL"})
    elif defect == "duplicate-identical":
        report["journeys"].append({"id": "J07-stop-model", "status": "PASS"})
    elif defect == "invalid-row":
        report["journeys"].append({"status": "FAIL"})
    elif defect == "non-object-row":
        report["journeys"].append(None)
    preflight = tmp_path / "preflight.json"
    preflight.write_text(json.dumps(prepared), encoding="utf-8")
    journeys = tmp_path / "journeys.json"
    journeys.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")

    def git(_repo, *args):
        if "rev-parse" in args:
            return "other" if defect == "head" else "abc"
        return " M changed.cpp" if defect == "dirty" else ""

    monkeypatch.setattr(module, "_git", git)
    output = tmp_path / "acceptance.json"
    with pytest.raises(module.AcceptanceCaptureError, match=reason):
        module.finalize_qualification_run(preflight, journeys, output, "qualify")
    assert not output.exists()


def test_composed_run_retains_logs_but_refuses_finalization_after_network_returns(
    tmp_path, monkeypatch
):
    module = _load()
    output = tmp_path / "output"

    def prepare(*_args):
        output.mkdir()
        return {"ready_to_disconnect": True, "blockers": []}

    monkeypatch.setattr(module, "prepare_qualification", prepare)
    monkeypatch.setattr(module, "capture_preflight", lambda *_args: {})
    monkeypatch.setattr(module, "_run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout="retained", stderr=""))
    monkeypatch.setattr(module, "active_default_routes", lambda: [{"InterfaceAlias": "Wi-Fi"}])
    monkeypatch.setattr("builtins.input", lambda *_args: "Wi-Fi disabled")

    def forbidden(*_args):
        pytest.fail("reconnected qualification cannot be finalized")

    monkeypatch.setattr(module, "finalize_qualification_run", forbidden)
    with pytest.raises(module.AcceptanceCaptureError, match="active default route"):
        module.run_qualification(
            tmp_path, tmp_path, output, "ptl-gpu-30b",
            confirm_disconnected=True,
        )
    assert (output / "journeys.stdout.log").read_text(encoding="utf-8") == "retained"
    assert not (output / "acceptance.json").exists()


def test_finalize_refuses_checkout_drift_before_accepting_evidence(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "schema": module.SCHEMA,
                "phase": "preflight",
                "repository": {"root": str(repo), "head": "expected", "clean": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "_git", lambda *_args: "different")

    with pytest.raises(module.AcceptanceCaptureError, match="checkout drifted"):
        module.finalize(preflight, tmp_path / "final.json", [], 100, 50)


def test_finalize_requires_positive_timings_and_real_evidence(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "schema": module.SCHEMA,
                "phase": "preflight",
                "repository": {"root": str(repo), "head": "same", "clean": True},
                "claim": "preflight-only",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "_git", lambda *_args: "same" if "rev-parse" in _args else "")
    monkeypatch.setattr(module, "active_default_routes", lambda: [])

    with pytest.raises(module.AcceptanceCaptureError, match="must be positive"):
        module.finalize(preflight, tmp_path / "final.json", [], 0, 50)

    with pytest.raises(module.AcceptanceCaptureError, match="evidence file is required"):
        module.finalize(preflight, tmp_path / "final.json", [], 100, 50)


def test_finalize_hashes_evidence_but_does_not_self_certify(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    preflight = tmp_path / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "schema": module.SCHEMA,
                "phase": "preflight",
                "repository": {"root": str(repo), "head": "same", "clean": True},
                "claim": "preflight-only",
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "journey.log"
    evidence.write_text("public Session Hub journey evidence\n", encoding="utf-8")
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "_git", lambda *_args: "same" if "rev-parse" in _args else "")
    monkeypatch.setattr(module, "active_default_routes", lambda: [])

    output = tmp_path / "final.json"
    payload = module.finalize(preflight, output, [evidence], 1200, 400)

    assert payload["phase"] == "captured"
    assert payload["latency_ms"] == {"cold": 1200, "warm": 400}
    assert payload["journey_evidence"][0]["sha256"] == module._sha256(evidence)
    assert payload["self_certified"] is False
    assert "human review" in payload["claim"]
    assert json.loads(output.read_text(encoding="utf-8"))["self_certified"] is False
