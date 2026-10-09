from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "panther-lake-acceptance.py"
SPEC = importlib.util.spec_from_file_location("panther_lake_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def _write_bundle(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "acceptance.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_summary_projects_only_observed_bundle_facts(tmp_path: Path) -> None:
    path = _write_bundle(
        tmp_path,
        {
            "schema": acceptance.SCHEMA,
            "phase": "captured",
            "repository": {"head": "1234567890abcdef", "clean": True},
            "offline_gate": {"passed": True},
            "profile": "ptl-npu-8b",
            "runtime": {"name": "OpenVINO", "device": "NPU"},
            "model_manifest_summary": {"source_model": "Qwen"},
            "qualification_summary": {"ok": True},
            "latency_ms": {"cold": 1200, "warm": 450},
        },
    )

    summary = acceptance.render_summary(path)

    assert "Checkout        1234567890ab" in summary
    assert "Offline gate    VERIFIED" in summary
    assert "Runtime         OpenVINO" in summary
    assert "Device config   NPU" in summary
    assert "Device observed UNKNOWN" in summary
    assert "Cold latency ms 1200" in summary
    assert "Build journey   UNKNOWN" in summary
    assert "Stop            UNKNOWN" in summary
    assert "Overall         INCOMPLETE - HUMAN REVIEW REQUIRED" in summary
    assert hashlib.sha256(path.read_bytes()).hexdigest() in summary


def test_summary_does_not_promote_preflight_to_acceptance(tmp_path: Path) -> None:
    path = _write_bundle(
        tmp_path,
        {
            "schema": acceptance.SCHEMA,
            "phase": "preflight",
            "repository": {"head": "abcdef", "clean": True},
            "offline_gate": {"passed": True},
            "profile": "ptl-npu-8b",
            "runtime": {},
            "model_manifest_summary": {},
            "qualification_summary": {},
        },
    )

    summary = acceptance.render_summary(path)

    assert "Cold latency ms UNKNOWN" in summary
    assert "Warm latency ms UNKNOWN" in summary
    assert "Device observed UNKNOWN" in summary
    assert "Overall         INCOMPLETE - PREFLIGHT ONLY" in summary


def test_summary_refuses_wrong_schema(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, {"schema": "old", "phase": "captured"})

    with pytest.raises(acceptance.AcceptanceCaptureError, match="schema"):
        acceptance.render_summary(path)


def test_prepare_reports_missing_weights_without_certifying_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "local-code-agent.ps1").write_text("launcher", encoding="utf-8")
    output = tmp_path / "evidence"
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Windows")
    monkeypatch.setattr(acceptance, "weight_state", lambda profile, root: "missing")
    monkeypatch.setattr(acceptance, "_git", lambda repo, *args: "abc123")
    observed: dict[str, object] = {}

    def run(args, *, cwd=None, timeout_s=None):
        observed.update(args=args, cwd=cwd, timeout_s=timeout_s)
        return SimpleNamespace(
            returncode=3,
            stdout=(
                "Weights        MISSING\n"
                "               next:       .\\local-code-agent.ps1 models pull ptl-gpu-30b\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(acceptance, "_run", run)

    report = acceptance.prepare_qualification(repo, tmp_path / "runtime", output, "ptl-gpu-30b")

    assert report["ready_to_disconnect"] is False
    assert report["offline_evidence"] == acceptance.UNKNOWN
    assert report["configured_device"] == "GPU"
    assert report["quantization"] == "INT4_ASYM"
    assert len(report["blockers"]) == 1
    assert "models pull ptl-gpu-30b" in report["blockers"][0]
    assert observed["args"] == [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(repo.resolve() / "local-code-agent.ps1"),
        "doctor", "--repo", str(repo.resolve()), "--profile", "ptl-gpu-30b",
    ]
    assert observed["cwd"] == repo.resolve()
    assert observed["timeout_s"] == 120.0
    retained = json.loads((output / "qualification-preflight.json").read_text(encoding="utf-8"))
    assert retained["claim"].startswith("preparation only")


def test_prepare_refuses_to_overwrite_an_evidence_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Windows")
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(acceptance.AcceptanceCaptureError, match="already exists"):
        acceptance.prepare_qualification(tmp_path, tmp_path, output, "ptl-npu-8b")
