from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "finalize_change.py"
SPEC = importlib.util.spec_from_file_location("lca_finalize_change", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
finalize_change = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalize_change)


def _valid_declared() -> dict[str, str]:
    return {
        "base_prompt_sha256": "b" * 64,
        "outcome_contract_sha256": "c" * 64,
    }


def _actual(**overrides: str) -> dict[str, str]:
    value = {
        "source_sha256": "a" * 64,
        "base_prompt_sha256": "b" * 64,
        "outcome_contract_sha256": "c" * 64,
    }
    value.update(overrides)
    return value


def test_contract_drift_ignores_source_identity_moves():
    declared = _valid_declared()
    actual = _actual(source_sha256="d" * 64)

    assert finalize_change.contract_drift(declared, actual) == {}


def test_contract_drift_reports_only_frozen_axes():
    declared = _valid_declared()
    actual = _actual(base_prompt_sha256="e" * 64)

    assert finalize_change.contract_drift(declared, actual) == {
        "base_prompt_sha256": ("b" * 64, "e" * 64)
    }


def test_load_declaration_rejects_live_source_hash(tmp_path):
    path = tmp_path / "INSTRUMENT.json"
    value = {**_valid_declared(), "source_sha256": "a" * 64}
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(finalize_change.FinalizationError, match="derived from the exact tree"):
        finalize_change.load_declaration(path)


def test_load_declaration_rejects_invalid_contract_fields(tmp_path):
    path = tmp_path / "INSTRUMENT.json"
    value = _valid_declared()
    value["base_prompt_sha256"] = "not-a-sha"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(finalize_change.FinalizationError, match="base_prompt_sha256"):
        finalize_change.load_declaration(path)


def test_load_declaration_rejects_non_object_json(tmp_path):
    path = tmp_path / "INSTRUMENT.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(finalize_change.FinalizationError, match="JSON object"):
        finalize_change.load_declaration(path)


def test_main_refuses_contract_drift(monkeypatch):
    declared = _valid_declared()
    actual = _actual(base_prompt_sha256="e" * 64)

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)

    assert finalize_change.main([]) == 2


def test_main_accepts_source_only_drift_without_writing(monkeypatch):
    declared = _valid_declared()
    actual = _actual(source_sha256="d" * 64)
    printed_identities: list[str] = []

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)
    monkeypatch.setattr(
        finalize_change,
        "_print_identities",
        lambda values: printed_identities.append(values["source_sha256"]),
    )

    assert finalize_change.main([]) == 0
    assert printed_identities == ["d" * 64]


def test_legacy_write_source_flag_is_harmless_compatibility(monkeypatch):
    declared = _valid_declared()
    actual = _actual(source_sha256="d" * 64)

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)
    monkeypatch.setattr(finalize_change, "_print_identities", lambda values: None)

    assert finalize_change.main(["--write-source"]) == 0


def test_main_fails_closed_when_identity_computation_is_unavailable(monkeypatch):
    monkeypatch.setattr(finalize_change, "load_declaration", _valid_declared)

    def unavailable():
        raise OSError("tree disappeared")

    monkeypatch.setattr(finalize_change, "compute_identities", unavailable)

    assert finalize_change.main([]) == 3
