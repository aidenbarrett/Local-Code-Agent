#!/usr/bin/env python3
"""Capture reproducible evidence for the physical Panther Lake acceptance run.

This helper deliberately does not certify NPU support. It creates a bounded evidence
bundle around a human-run public Session Hub rehearsal and fails closed when the checkout,
offline state, or required runtime provenance is not established.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent.config import MODEL_PRESETS  # noqa: E402
from scripts.model_weights import weight_state  # noqa: E402
from serving import serve  # noqa: E402
from serving.model_store import default_runtime_root  # noqa: E402

SCHEMA = "lca.panther-lake-acceptance/1"
UNKNOWN = "UNKNOWN"
PREPARE_SCHEMA = "lca.offline-qualification-preflight/1"
_make_plan: Callable[..., dict[str, Any]] = serve.make_plan


class AcceptanceCaptureError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(
    args: list[str], *, cwd: Path | None = None, timeout_s: float | None = None,
    runtime_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LCA_RUNTIME_ROOT": str(runtime_root.resolve())} if runtime_root else None
    return subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout_s, env=env
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", "-C", str(repo), *args])
    if result.returncode != 0:
        raise AcceptanceCaptureError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _powershell_json(script: str) -> Any:
    result = _run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script
    ])
    if result.returncode != 0:
        raise AcceptanceCaptureError(result.stderr.strip() or "PowerShell probe failed")
    text = result.stdout.strip()
    return None if not text else json.loads(text)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def active_default_routes() -> list[dict[str, Any]]:
    script = r'''
$active = Get-NetRoute -ErrorAction Stop |
    Where-Object { ($_.DestinationPrefix -eq '0.0.0.0/0' -or $_.DestinationPrefix -eq '::/0') -and $_.State -eq 'Alive' } |
    ForEach-Object {
        $adapter = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction Stop
        if (-not $adapter) { throw 'Cannot observe the default route adapter' }
        if ($adapter.Status -eq 'Up') {
            [pscustomobject]@{DestinationPrefix=$_.DestinationPrefix;InterfaceIndex=$_.InterfaceIndex;InterfaceAlias=$adapter.Name}
        }
    }
ConvertTo-Json -InputObject @($active) -Compress
'''
    routes = _powershell_json(script)
    if not isinstance(routes, list) or any(not isinstance(row, dict) for row in routes):
        raise AcceptanceCaptureError("default route probe did not return an observed route list")
    return [dict(item) for item in routes]


def require_offline(routes: Iterable[dict[str, Any]]) -> None:
    routes = list(routes)
    if routes:
        names = ", ".join(str(row.get("InterfaceAlias", row.get("InterfaceIndex", "?"))) for row in routes)
        raise AcceptanceCaptureError(
            "external-network gate refused: active default route(s) remain on " + names
        )


def configured_devices(device: str) -> list[dict[str, Any]]:
    if device == "NPU":
        predicate = "Test-LcaNpuDeviceName $_.FriendlyName"
    elif device == "GPU":
        predicate = "$_.Class -eq 'Display'"
    else:
        raise AcceptanceCaptureError(f"unsupported Panther Lake device probe: {device}")
    script = r'''
$devices = Get-PnpDevice -PresentOnly -ErrorAction Stop |
    Where-Object { __PREDICATE__ } |
    ForEach-Object {
        $device = $_
        $drv = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
            Where-Object { $_.DeviceID -eq $device.InstanceId } | Select-Object -First 1
        [pscustomobject]@{name=$device.FriendlyName;status=$device.Status;instance_id=$device.InstanceId;driver_version=$drv.DriverVersion;driver_provider=$drv.DriverProviderName}
    }
@($devices) | ConvertTo-Json -Compress
'''
    script = script.replace("__PREDICATE__", predicate)
    if device == "NPU":
        helper = str(INTERNAL / "scripts" / "windows-npu-device.ps1").replace("'", "''")
        script = f". '{helper}'\n" + script
    return [dict(item) for item in _as_list(_powershell_json(script))]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise AcceptanceCaptureError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceCaptureError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceCaptureError(f"{label} must contain a JSON object")
    return value


def _artifact(path: Path) -> dict[str, Any]:
    return {"path": str(path), "sha256": _sha256(path), "bytes": path.stat().st_size}


def installed_runtime_evidence(profile: str, runtime_root: Path) -> dict[str, Any]:
    """Consume the serving/install owners' current artifacts, never legacy profiles."""
    config = MODEL_PRESETS[profile]
    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    plan = _make_plan(profile, config, runtime_root, executable=executable)
    launch_path = Path(plan["spec_file"])
    launch = _load_json(launch_path, "managed launch plan")
    if (launch.get("profile") != profile
            or launch.get("model_configuration") != config.identity()
            or serve.launch_differences(launch, plan)):
        raise AcceptanceCaptureError("managed launch plan differs from the selected profile")
    qualification_path = runtime_root / "reports" / f"qualification-{profile}.json"
    qualification = _load_json(qualification_path, "installation qualification report")
    if (qualification.get("schema_version") != 1
            or qualification.get("profile") != profile
            or qualification.get("model") != config.model
            or not isinstance(qualification.get("base_url"), str)
            or qualification["base_url"].rstrip("/") != config.base_url.rstrip("/")):
        raise AcceptanceCaptureError(
            "installation qualification identity/schema differs from profile"
        )
    checks = qualification.get("checks")
    required = {"configured model present", "plain chat", "schema constrained tool call"}
    if (qualification.get("failed") is not False or not isinstance(checks, list)
            or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                   or not isinstance(row.get("status"), str)
                   or row["status"] not in {"PASS", "SKIP"}
                   for row in checks)
            or not required.issubset({row.get("name") for row in checks
                                     if row.get("status") == "PASS"})):
        raise AcceptanceCaptureError("installation qualification is incomplete or failed")
    return {
        "runtime": {"runtime": config.runtime, "device": config.device,
                    "version": config.runtime_version, "launch_plan": launch,
                    "claim": "recorded launch configuration; physical device execution unobserved"},
        "qualification_summary": {"ok": True, "model": config.model,
                                  "scope": "retained installation protocol checks"},
        "artifacts": {"launch_plan": _artifact(launch_path),
                      "qualification": _artifact(qualification_path)},
    }


def prepare_qualification(
    repo: Path, runtime_root: Path, output: Path, profile: str
) -> dict[str, Any]:
    """Check local prerequisites before the user disconnects the target machine."""
    if platform.system() != "Windows":
        raise AcceptanceCaptureError("physical qualification preparation must run on Windows")
    if profile not in MODEL_PRESETS:
        raise AcceptanceCaptureError(
            f"unknown profile {profile}; run .\\local-code-agent.ps1 models"
        )
    if not profile.startswith("ptl-"):
        raise AcceptanceCaptureError("physical Panther Lake qualification requires a ptl-* profile")
    if output.exists():
        raise AcceptanceCaptureError(f"output folder already exists: {output}")

    repo = repo.resolve()
    runtime_root = runtime_root.resolve()
    config = MODEL_PRESETS[profile]
    blockers: list[str] = []
    state = weight_state(profile, runtime_root)

    launcher = repo / "local-code-agent.ps1"
    doctor_args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(launcher), "doctor", "--repo", str(repo),
        "--profile", profile,
    ]
    try:
        doctor = _run(doctor_args, cwd=repo, timeout_s=120.0, runtime_root=runtime_root)
    except subprocess.TimeoutExpired:
        doctor = subprocess.CompletedProcess(
            doctor_args, 124, stdout="", stderr="doctor timed out after 120 seconds"
        )
    if doctor.returncode != 0:
        detail = doctor.stderr.strip() or doctor.stdout.strip() or f"exit {doctor.returncode}"
        blockers.append("doctor is not ready: " + detail[-1000:])
    else:
        try:
            installed_runtime_evidence(profile, runtime_root)
        except (AcceptanceCaptureError, serve.Refusal) as exc:
            blockers.append(
                f"{exc}; run .\\local-code-agent.ps1 models use {profile}, then "
                ".\\install.ps1 before disconnecting"
            )

    payload = {
        "schema": PREPARE_SCHEMA,
        "phase": "prepare",
        "captured_at_utc": _now(),
        "repository": {"root": str(repo), "head": _git(repo, "rev-parse", "HEAD")},
        "profile": profile,
        "model": config.model,
        "quantization": config.quant,
        "runtime": {"name": config.runtime, "version": config.runtime_version},
        "configured_device": config.device,
        "model_weights": state,
        "doctor": {"exit_code": doctor.returncode, "stdout": doctor.stdout[-4000:]},
        "offline_evidence": UNKNOWN,
        "blockers": blockers,
        "ready_to_disconnect": not blockers,
        "claim": (
            "preparation only; network isolation, physical execution and acceptance "
            "are not certified"
        ),
    }
    output.mkdir(parents=True)
    (output / "qualification-preflight.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def capture_preflight(
    repo: Path, runtime_root: Path, output: Path, profile: str
) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise AcceptanceCaptureError("Panther Lake physical acceptance must run on Windows")
    repo = repo.resolve()
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise AcceptanceCaptureError("acceptance checkout must be clean; git status is not empty")

    preparation = _load_json(
        output.parent / "qualification-preflight.json", "qualification preparation"
    )
    if preparation.get("schema") != PREPARE_SCHEMA or preparation.get("phase") != "prepare":
        raise AcceptanceCaptureError("qualification preparation schema/phase is not current")
    if preparation.get("profile") != profile:
        raise AcceptanceCaptureError("selected profile does not match qualification preparation")
    prepared_repo = preparation.get("repository")
    prepared_head = prepared_repo.get("head") if isinstance(prepared_repo, dict) else None
    if prepared_head != head:
        raise AcceptanceCaptureError("checkout drifted after qualification preparation")

    routes = active_default_routes()
    require_offline(routes)
    if profile not in MODEL_PRESETS:
        raise AcceptanceCaptureError(
            f"unknown profile {profile}; run .\\local-code-agent.ps1 models"
        )
    if not profile.startswith("ptl-"):
        raise AcceptanceCaptureError("physical Panther Lake qualification requires a ptl-* profile")
    config = MODEL_PRESETS[profile]
    devices = configured_devices(config.device)
    if not devices:
        raise AcceptanceCaptureError(
            f"no present Windows {config.device} device matched the approved probe"
        )
    if weight_state(profile, runtime_root) != "downloaded":
        raise AcceptanceCaptureError(f"model weights for {profile} are not complete")
    installed = installed_runtime_evidence(profile, runtime_root)

    launcher = repo / "local-code-agent.ps1"
    if not launcher.is_file():
        raise AcceptanceCaptureError("public launcher is missing")
    check = _run([
        "powershell.exe", "-NoProfile", "-File", str(launcher),
        "session", "--check", "--repo", str(repo), "--profile", profile,
    ], cwd=repo, runtime_root=runtime_root)
    if check.returncode != 0:
        raise AcceptanceCaptureError(
            "public Session Hub --check failed: " + (check.stderr.strip() or check.stdout.strip())
        )

    payload = {
        "schema": SCHEMA,
        "phase": "preflight",
        "captured_at_utc": _now(),
        "repository": {"root": str(repo), "head": head, "clean": True},
        "offline_gate": {"method": "no alive default route on an Up Windows adapter", "passed": True, "routes": routes},
        "profile": profile,
        "device_candidates": devices,
        "runtime": installed["runtime"],
        "model_manifest_summary": {
            "source_model": config.model,
            "weights": "downloaded",
        },
        "qualification_summary": installed["qualification_summary"],
        "artifacts": installed["artifacts"],
        "public_check": {"exit_code": check.returncode, "stdout": check.stdout[-4000:]},
        "claim": "preflight-only; physical Session Hub journeys have not been accepted",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def finalize(preflight_path: Path, output: Path, evidence: list[Path], cold_ms: int, warm_ms: int) -> dict[str, Any]:
    preflight = _load_json(preflight_path, "acceptance preflight")
    if preflight.get("schema") != SCHEMA or preflight.get("phase") != "preflight":
        raise AcceptanceCaptureError(
            "preflight schema/phase is not the current acceptance contract"
        )
    repo = Path(preflight["repository"]["root"])
    if platform.system() != "Windows":
        raise AcceptanceCaptureError("Panther Lake physical acceptance must finalize on Windows")
    if _git(repo, "rev-parse", "HEAD") != preflight["repository"]["head"]:
        raise AcceptanceCaptureError("checkout drifted after preflight")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise AcceptanceCaptureError("checkout became dirty after preflight")
    require_offline(active_default_routes())
    if cold_ms <= 0 or warm_ms <= 0:
        raise AcceptanceCaptureError("cold and warm latency measurements must be positive")
    if not evidence:
        raise AcceptanceCaptureError("at least one journey evidence file is required")
    missing = [str(path) for path in evidence if not path.is_file()]
    if missing:
        raise AcceptanceCaptureError("missing journey evidence: " + ", ".join(missing))

    payload = {
        **preflight,
        "phase": "captured",
        "finalized_at_utc": _now(),
        "latency_ms": {"cold": cold_ms, "warm": warm_ms},
        "journey_evidence": [_artifact(path.resolve()) for path in evidence],
        "claim": ("evidence captured; support claim requires human review against "
                  "docs/development/TRICKS.md"),
        "self_certified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def finalize_qualification_run(
    preflight_path: Path, journeys_path: Path, output: Path, command: str
) -> dict[str, Any]:
    """Bind the existing acceptance owner's output to the physical preflight.

    The projection records outcomes; it never upgrades configured hardware into
    observed execution or grades the physical evidence itself.
    """
    preflight = _load_json(preflight_path, "acceptance preflight")
    journeys = _load_json(journeys_path, "acceptance journeys")
    if preflight.get("schema") != SCHEMA or preflight.get("phase") != "preflight":
        raise AcceptanceCaptureError("preflight schema/phase is not the current acceptance contract")
    if journeys.get("profile") != preflight.get("profile"):
        raise AcceptanceCaptureError("acceptance profile does not match disconnected preflight")
    repository = preflight.get("repository")
    if not isinstance(repository, dict):
        raise AcceptanceCaptureError("preflight repository identity is missing")
    repo = Path(repository["root"])
    if platform.system() != "Windows":
        raise AcceptanceCaptureError("Panther Lake physical acceptance must finalize on Windows")
    if _git(repo, "rev-parse", "HEAD") != repository.get("head"):
        raise AcceptanceCaptureError("checkout drifted during qualification run")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise AcceptanceCaptureError("checkout became dirty during qualification run")
    rows = journeys.get("journeys")
    if not isinstance(rows, list) or not rows:
        raise AcceptanceCaptureError("acceptance report contains no journey outcomes")
    by_id = {
        row.get("id"): row for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    required = ("J05-stop-build", "J07-stop-model", "J24-continuous")
    missing = [journey_id for journey_id in required if journey_id not in by_id]
    if missing:
        raise AcceptanceCaptureError(
            "acceptance report is missing required journeys: " + ", ".join(missing)
        )
    preconditions = journeys.get("preconditions")
    preconditions = preconditions if isinstance(preconditions, dict) else {}
    product = preconditions.get("product")
    if not isinstance(product, dict) or product.get("package_commit") != repository.get("head"):
        raise AcceptanceCaptureError(
            "acceptance source identity does not match disconnected preflight"
        )
    endpoint = preconditions.get("model_endpoint")
    endpoint = endpoint if isinstance(endpoint, dict) else {}
    model_call = preconditions.get("model_call")
    model_call = model_call if isinstance(model_call, dict) else {}

    def measured(journey_id: str) -> dict[str, Any]:
        value = by_id[journey_id].get("measured")
        return value if isinstance(value, dict) else {}

    payload = {
        **preflight,
        "phase": "captured",
        "finalized_at_utc": _now(),
        "run_evidence": {
            "journeys": {
                journey_id: {"status": row.get("status"), "reason": row.get("reason")}
                for journey_id, row in by_id.items()
            },
            "stop": {
                "build": measured("J05-stop-build"),
                "model": measured("J07-stop-model"),
            },
            "continuous_workflow": measured("J24-continuous"),
            "runtime_lifecycle": {
                "lifecycle": endpoint.get("lifecycle", UNKNOWN),
                "launch_to_ready_ms": endpoint.get("launch_to_ready_ms", UNKNOWN),
                "first_model_call_s": model_call.get("seconds", UNKNOWN),
                "cold_model_ms": UNKNOWN,
                "warm_model_ms": UNKNOWN,
            },
        },
        "journey_evidence": [_artifact(journeys_path.resolve())],
        "reproduction": command,
        "claim": (
            "scripted evidence captured; physical device execution and support "
            "require human review"
        ),
        "self_certified": False,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def run_qualification(
    repo: Path, runtime_root: Path, output: Path, profile: str, *, confirm_disconnected: bool
) -> dict[str, Any]:
    preparation = prepare_qualification(repo, runtime_root, output, profile)
    _print_preparation(preparation)
    if not preparation["ready_to_disconnect"]:
        blockers = "; ".join(str(value) for value in preparation["blockers"])
        raise AcceptanceCaptureError("not ready to disconnect: " + blockers)
    if not confirm_disconnected:
        response = input(
            "Disconnect external networking, then type DISCONNECTED to continue: "
        ).strip()
        if response != "DISCONNECTED":
            raise AcceptanceCaptureError(
                "disconnected run cancelled; exact confirmation was not supplied"
            )

    preflight_path = output / "offline-preflight.json"
    capture_preflight(repo, runtime_root, preflight_path, profile)
    journeys_dir = output / "journeys"
    launcher = repo.resolve() / "local-code-agent.ps1"
    sys.stdout.write(f"Running public acceptance journeys; evidence: {journeys_dir}\n")
    sys.stdout.flush()
    acceptance = _run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(launcher), "acceptance", "--output", str(journeys_dir),
            "--profile", profile, "--allow-model",
        ],
        cwd=repo.resolve(),
        runtime_root=runtime_root,
    )
    (output / "journeys.stdout.log").write_text(acceptance.stdout, encoding="utf-8")
    (output / "journeys.stderr.log").write_text(acceptance.stderr, encoding="utf-8")
    post_run_routes = active_default_routes()
    require_offline(post_run_routes)
    journeys_path = journeys_dir / "journeys.json"
    command = (
        f".\\local-code-agent.ps1 qualify --profile {profile} "
        "--output <new-output-folder>"
    )
    payload = finalize_qualification_run(
        preflight_path, journeys_path, output / "acceptance.json", command
    )
    payload["post_run_offline_gate"] = {
        "method": "no alive default route on an Up Windows adapter",
        "passed": True,
        "routes": post_run_routes,
    }
    (output / "acceptance.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sys.stdout.write(write_summary(output / "acceptance.json", output / "summary.txt"))
    if acceptance.returncode != 0:
        raise AcceptanceCaptureError(
            f"acceptance journeys reported failure (exit {acceptance.returncode}); "
            "retained report is complete"
        )
    return payload


def _print_preparation(payload: dict[str, Any]) -> None:
    message = "READY TO DISCONNECT" if payload["ready_to_disconnect"] else "NOT READY"
    sys.stdout.write(message + "\n")
    for blocker in payload["blockers"]:
        sys.stdout.write(f"NEXT: {blocker}\n")
    sys.stdout.flush()



def render_summary(acceptance_path: Path) -> str:
    """Render a compact, photographable projection of captured evidence.

    This deliberately does not grade manual journeys or infer physical execution.
    UNKNOWN is the only value for a fact the bundle does not establish.
    """
    payload = _load_json(acceptance_path, "acceptance evidence")
    if payload.get("schema") != SCHEMA:
        raise AcceptanceCaptureError("acceptance evidence schema is not the current contract")

    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    offline = payload.get("offline_gate") if isinstance(payload.get("offline_gate"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    model_value = payload.get("model_manifest_summary")
    model = model_value if isinstance(model_value, dict) else {}
    qualification_value = payload.get("qualification_summary")
    qualification = qualification_value if isinstance(qualification_value, dict) else {}
    latency = payload.get("latency_ms") if isinstance(payload.get("latency_ms"), dict) else {}
    run_evidence = payload.get("run_evidence") if isinstance(payload.get("run_evidence"), dict) else {}
    runtime_lifecycle = run_evidence.get("runtime_lifecycle")
    runtime_lifecycle = runtime_lifecycle if isinstance(runtime_lifecycle, dict) else {}
    outcomes = run_evidence.get("journeys")
    outcomes = outcomes if isinstance(outcomes, dict) else {}

    def shown(value: Any) -> str:
        if value is None or value == "":
            return UNKNOWN
        if isinstance(value, bool):
            return "VERIFIED" if value else "FAILED"
        return str(value)

    def outcome_status(journey_id: str) -> Any:
        row = outcomes.get(journey_id)
        return row.get("status") if isinstance(row, dict) else None

    # Retained launch configuration is distinct from physical execution proof.
    runtime_name = runtime.get("runtime") or runtime.get("name")
    configured_device = runtime.get("device") or runtime.get("requested_device")
    source_model = model.get("source_model")
    head = repository.get("head")
    short_head = head[:12] if isinstance(head, str) and head else UNKNOWN
    phase = payload.get("phase")
    captured = phase == "captured"

    lines = [
        "PANTHER LAKE OFFLINE ACCEPTANCE",
        "",
        f"Checkout        {short_head}",
        f"Clean checkout  {shown(repository.get('clean'))}",
        f"Offline gate    {shown(offline.get('passed'))}",
        f"Profile         {shown(payload.get('profile'))}",
        f"Model           {shown(source_model)}",
        f"Runtime         {shown(runtime_name)}",
        f"Device config   {shown(configured_device)}",
        f"Device observed {UNKNOWN}",
        f"Qualification   {shown(qualification.get('ok'))}",
        f"Cold latency ms {shown(latency.get('cold')) if captured else UNKNOWN}",
        f"Warm latency ms {shown(latency.get('warm')) if captured else UNKNOWN}",
        f"Lifecycle       {shown(runtime_lifecycle.get('lifecycle'))}",
        f"Launch-ready ms {shown(runtime_lifecycle.get('launch_to_ready_ms'))}",
        f"First call s    {shown(runtime_lifecycle.get('first_model_call_s'))}",
        f"Cold model ms   {shown(runtime_lifecycle.get('cold_model_ms'))}",
        f"Warm model ms   {shown(runtime_lifecycle.get('warm_model_ms'))}",
        "",
        "SCRIPTED JOURNEYS (physical review required)",
        f"Build journey   {UNKNOWN}",
        f"Test journey    {UNKNOWN}",
        f"Candidate       {UNKNOWN}",
        f"Dirty checkout  {UNKNOWN}",
        f"Stop            {UNKNOWN if not outcomes else 'SEE RETAINED OUTCOMES'}",
        f"Stop build      {shown(outcome_status('J05-stop-build'))}",
        f"Stop model      {shown(outcome_status('J07-stop-model'))}",
        f"Continuous flow {shown(outcome_status('J24-continuous'))}",
        "",
        f"Evidence bundle {_sha256(acceptance_path)}",
        "Overall         "
        + ("INCOMPLETE - HUMAN REVIEW REQUIRED" if captured
           else "INCOMPLETE - PREFLIGHT ONLY"),
    ]
    return "\n".join(lines) + "\n"


def write_summary(acceptance_path: Path, output: Path) -> str:
    summary = render_summary(acceptance_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(summary, encoding="utf-8")
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--repo", type=Path, required=True)
    prepare.add_argument("--profile", required=True, choices=sorted(MODEL_PRESETS))
    prepare.add_argument("--runtime-root", type=Path, default=None)
    prepare.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--profile", required=True, choices=sorted(MODEL_PRESETS))
    run.add_argument("--runtime-root", type=Path, default=None)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--confirm-disconnected", action="store_true", help=argparse.SUPPRESS)
    pre = sub.add_parser("preflight")
    pre.add_argument("--repo", type=Path, required=True)
    pre.add_argument("--profile", required=True, choices=sorted(MODEL_PRESETS))
    pre.add_argument("--runtime-root", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    fin = sub.add_parser("finalize")
    fin.add_argument("--preflight", type=Path, required=True)
    fin.add_argument("--output", type=Path, required=True)
    fin.add_argument("--cold-latency-ms", type=int, required=True)
    fin.add_argument("--warm-latency-ms", type=int, required=True)
    fin.add_argument("--evidence", type=Path, action="append", default=[], required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("--acceptance", type=Path, required=True)
    summary.add_argument("--output", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            run_qualification(
                args.repo, args.runtime_root or default_runtime_root(), args.output,
                args.profile, confirm_disconnected=args.confirm_disconnected,
            )
        elif args.command == "prepare":
            payload = prepare_qualification(
                args.repo, args.runtime_root or default_runtime_root(), args.output, args.profile
            )
            _print_preparation(payload)
            return 0 if payload["ready_to_disconnect"] else 3
        elif args.command == "preflight":
            capture_preflight(args.repo, args.runtime_root, args.output, args.profile)
        elif args.command == "finalize":
            finalize(args.preflight, args.output, args.evidence, args.cold_latency_ms, args.warm_latency_ms)
        else:
            summary = write_summary(args.acceptance, args.output) if args.output else render_summary(args.acceptance)
            print(summary, end="")
    except (AcceptanceCaptureError, EOFError, OSError, KeyError, TypeError, ValueError,
            json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
