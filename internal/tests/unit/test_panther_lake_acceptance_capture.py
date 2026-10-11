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


def test_preflight_parser_requires_and_retains_selected_profile():
    module = _load()
    args = module.parser().parse_args(
        [
            "preflight",
            "--repo", ".",
            "--profile", "ptl-gpu-30b",
            "--runtime-root", "runtime",
            "--output", "preflight.json",
        ]
    )

    assert args.profile == "ptl-gpu-30b"


def test_preflight_uses_selected_profile_for_every_owned_artifact(tmp_path, monkeypatch):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local-code-agent.ps1").write_text("launcher", encoding="utf-8")
    runtime_root = tmp_path / "runtime-root"
    runtime_profile = runtime_root / "runtime" / "profiles" / "ptl-gpu-30b.json"
    qualification = runtime_root / "reports" / "qualification-ptl-gpu-30b.json"
    runtime_profile.parent.mkdir(parents=True)
    qualification.parent.mkdir(parents=True)
    runtime_profile.write_text(json.dumps({"runtime": "ovms", "device": "GPU"}), encoding="utf-8")
    qualification.write_text(json.dumps({"ok": True, "model": "gpu-model"}), encoding="utf-8")
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
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(module, "_run", run)

    payload = module.capture_preflight(
        repo, runtime_root, output, "ptl-gpu-30b"
    )

    assert payload["profile"] == "ptl-gpu-30b"
    assert payload["runtime"]["device"] == "GPU"
    assert observed["device"] == "GPU"
    assert payload["model_manifest_summary"] == {
        "source_model": module.MODEL_PRESETS["ptl-gpu-30b"].model,
        "weights": "downloaded",
    }
    assert observed["args"][-2:] == ["--profile", "ptl-gpu-30b"]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_one_run_composes_existing_capture_and_acceptance_owners(
    tmp_path, monkeypatch, capsys, exit_code
):
    module = _load()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local-code-agent.ps1").write_text("launcher", encoding="utf-8")
    output = tmp_path / "qualification"
    observed = {}

    def prepare(_repo, _runtime, destination, profile):
        destination.mkdir()
        observed["prepared"] = profile
        return {"ready_to_disconnect": True, "blockers": []}

    def preflight(_repo, _runtime, destination, profile):
        payload = {
            "schema": module.SCHEMA,
            "phase": "preflight",
            "profile": profile,
            "repository": {"root": str(repo), "head": "abc", "clean": True},
            "offline_gate": {"passed": True},
        }
        destination.write_text(json.dumps(payload), encoding="utf-8")
        observed["preflight"] = profile
        return payload

    def run(args, **_kwargs):
        destination = Path(args[args.index("--output") + 1])
        destination.mkdir()
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
    monkeypatch.setattr(
        module, "_git", lambda _repo, *args: "abc" if "rev-parse" in args else ""
    )

    if exit_code:
        with pytest.raises(module.AcceptanceCaptureError, match="reported failure"):
            module.run_qualification(
                repo, tmp_path / "runtime", output, "ptl-gpu-30b", confirm_disconnected=True
            )
        payload = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
    else:
        payload = module.run_qualification(
            repo, tmp_path / "runtime", output, "ptl-gpu-30b", confirm_disconnected=True
        )

    assert observed["prepared"] == observed["preflight"] == "ptl-gpu-30b"
    assert observed["command"][-3:] == ["--profile", "ptl-gpu-30b", "--allow-model"]
    assert payload["self_certified"] is False
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
            tmp_path, tmp_path, tmp_path / "output", "ptl-gpu-30b", confirm_disconnected=False
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
    with pytest.raises(module.AcceptanceCaptureError, match="exact confirmation"):
        module.run_qualification(
            tmp_path, tmp_path, tmp_path / "output", "ptl-gpu-30b", confirm_disconnected=False
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
