from __future__ import annotations

import json

import pytest

from measurement.analyze_endpoints import _payload, main


IDENTITY = {
    "generation": 2,
    "source_sha256": "1" * 64,
    "base_prompt_sha256": "2" * 64,
    "outcome_contract_sha256": "3" * 64,
    "model": "model-a",
    "model_identity": {"model": "model-a", "runtime": "test"},
    "rehearsal": False,
}


def _write(path, *, condition="narrow", identity=None, row_identity=True, rehearsal=False):
    ident = dict(IDENTITY if identity is None else identity)
    ident["rehearsal"] = rehearsal
    row = {
        "case": "link-error",
        "condition": condition,
        "attempt": 0,
        "counted": False,
        "validity": "invalid_server_unavailable",
    }
    if row_identity:
        row.update(ident)
    payload = {
        **ident,
        "condition": condition,
        "complete": True,
        "attempts_declared": {"link-error": 1},
        "rows": [row],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_analysis_rejects_rehearsal_payload(tmp_path):
    path = _write(tmp_path / "r.json", rehearsal=True)
    with pytest.raises(ValueError, match="rehearsal data"):
        _payload(path)


def test_analysis_rejects_row_without_identity(tmp_path):
    path = _write(tmp_path / "r.json", row_identity=False)
    with pytest.raises(ValueError, match="identity mismatch"):
        _payload(path)


def test_analysis_rejects_mixed_model_configurations(tmp_path):
    a = _write(tmp_path / "a.json", condition="control")
    other = dict(IDENTITY)
    other["model_identity"] = {"model": "model-b", "runtime": "test"}
    other["model"] = "model-b"
    b = _write(tmp_path / "b.json", condition="narrow", identity=other)
    with pytest.raises(ValueError, match="mixed generation/model/instrument identity"):
        main([str(a), str(b)])
