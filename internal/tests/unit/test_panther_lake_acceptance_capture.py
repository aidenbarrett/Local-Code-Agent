from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

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
