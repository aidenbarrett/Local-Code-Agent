from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

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
    assert "Overall         INCOMPLETE — HUMAN REVIEW REQUIRED" in summary
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
    assert "Overall         INCOMPLETE — PREFLIGHT ONLY" in summary


def test_summary_refuses_wrong_schema(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, {"schema": "old", "phase": "captured"})

    with pytest.raises(acceptance.AcceptanceCaptureError, match="schema"):
        acceptance.render_summary(path)
