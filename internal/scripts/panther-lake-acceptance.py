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
from typing import Any, Iterable

PROFILE = "ptl-npu-8b"
SCHEMA = "lca.panther-lake-acceptance/1"


class AcceptanceCaptureError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


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
        $adapter = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue
        if ($adapter -and $adapter.Status -eq 'Up') {
            [pscustomobject]@{DestinationPrefix=$_.DestinationPrefix;InterfaceIndex=$_.InterfaceIndex;InterfaceAlias=$adapter.Name}
        }
    }
@($active) | ConvertTo-Json -Compress
'''
    return [dict(item) for item in _as_list(_powershell_json(script))]


def require_offline(routes: Iterable[dict[str, Any]]) -> None:
    routes = list(routes)
    if routes:
        names = ", ".join(str(row.get("InterfaceAlias", row.get("InterfaceIndex", "?"))) for row in routes)
        raise AcceptanceCaptureError(
            "external-network gate refused: active default route(s) remain on " + names
        )


def npu_devices() -> list[dict[str, Any]]:
    script = r'''
$devices = Get-PnpDevice -PresentOnly -ErrorAction Stop |
    Where-Object { $_.FriendlyName -match 'NPU|Neural Processing|AI Boost' } |
    ForEach-Object {
        $drv = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
            Where-Object { $_.DeviceID -eq $_.InstanceId } | Select-Object -First 1
        [pscustomobject]@{name=$_.FriendlyName;status=$_.Status;instance_id=$_.InstanceId;driver_version=$drv.DriverVersion;driver_provider=$drv.DriverProviderName}
    }
@($devices) | ConvertTo-Json -Compress
'''
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


def capture_preflight(repo: Path, runtime_root: Path, output: Path) -> dict[str, Any]:
    if platform.system() != "Windows":
        raise AcceptanceCaptureError("Panther Lake physical acceptance must run on Windows")
    repo = repo.resolve()
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise AcceptanceCaptureError("acceptance checkout must be clean; git status is not empty")

    routes = active_default_routes()
    require_offline(routes)
    devices = npu_devices()
    if not devices:
        raise AcceptanceCaptureError("no present Windows NPU device matched the approved probe")

    reports = runtime_root / "reports"
    runtime_profile = runtime_root / "runtime" / "profiles" / f"{PROFILE}.json"
    model_manifest = reports / "model-qwen3-8b-int4-cw-ov-manifest.json"
    qualification = reports / f"qualification-{PROFILE}.json"
    runtime = _load_json(runtime_profile, "managed runtime profile")
    model = _load_json(model_manifest, "model manifest")
    qual = _load_json(qualification, "qualification report")

    launcher = repo / "local-code-agent.ps1"
    if not launcher.is_file():
        raise AcceptanceCaptureError("public launcher is missing")
    check = _run([
        "powershell.exe", "-NoProfile", "-File", str(launcher),
        "session", "--check", "--repo", str(repo), "--profile", PROFILE,
    ], cwd=repo)
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
        "profile": PROFILE,
        "npu_devices": devices,
        "runtime": runtime,
        "model_manifest_summary": {
            "source_model": model.get("source_model"),
            "file_count": len(model.get("files", [])) if isinstance(model.get("files"), list) else None,
        },
        "qualification_summary": {"ok": qual.get("ok"), "model": qual.get("model")},
        "artifacts": {
            "runtime_profile": _artifact(runtime_profile),
            "model_manifest": _artifact(model_manifest),
            "qualification": _artifact(qualification),
        },
        "public_check": {"exit_code": check.returncode, "stdout": check.stdout[-4000:]},
        "claim": "preflight-only; physical Session Hub journeys have not been accepted",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def finalize(preflight_path: Path, output: Path, evidence: list[Path], cold_ms: int, warm_ms: int) -> dict[str, Any]:
    preflight = _load_json(preflight_path, "acceptance preflight")
    if preflight.get("schema") != SCHEMA or preflight.get("phase") != "preflight":
        raise AcceptanceCaptureError("preflight schema/phase is not the current acceptance contract")
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
        "claim": "evidence captured; support claim requires human review against TRICKS.md",
        "self_certified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--repo", type=Path, required=True)
    pre.add_argument("--runtime-root", type=Path, required=True)
    pre.add_argument("--output", type=Path, required=True)
    fin = sub.add_parser("finalize")
    fin.add_argument("--preflight", type=Path, required=True)
    fin.add_argument("--output", type=Path, required=True)
    fin.add_argument("--cold-latency-ms", type=int, required=True)
    fin.add_argument("--warm-latency-ms", type=int, required=True)
    fin.add_argument("--evidence", type=Path, action="append", default=[], required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "preflight":
            capture_preflight(args.repo, args.runtime_root, args.output)
        else:
            finalize(args.preflight, args.output, args.evidence, args.cold_latency_ms, args.warm_latency_ms)
    except (AcceptanceCaptureError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
