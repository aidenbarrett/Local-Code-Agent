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


def _declared() -> dict[str, str]:
    return {
        "source_sha256": "source-old",
        "base_prompt_sha256": "base",
        "outcome_contract_sha256": "outcome",
    }


def _valid_declared() -> dict[str, str]:
    return {
        "source_sha256": "a" * 64,
        "base_prompt_sha256": "b" * 64,
        "outcome_contract_sha256": "c" * 64,
    }


def test_identity_drift_reports_only_changed_axes():
    declared = _declared()
    actual = {
        "source_sha256": "source-new",
        "base_prompt_sha256": "base",
        "outcome_contract_sha256": "outcome",
    }

    assert finalize_change.identity_drift(declared, actual) == {
        "source_sha256": ("source-old", "source-new")
    }


def test_stamp_source_changes_only_source_identity(tmp_path):
    path = tmp_path / "INSTRUMENT.json"
    declared = {
        "generation": 2,
        **_declared(),
        "other": ["preserve", "me"],
    }
    path.write_text(json.dumps(declared, indent=2) + "\n", encoding="utf-8")

    finalize_change.stamp_source(path, declared, "source-new")

    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["source_sha256"] == "source-new"
    assert updated["base_prompt_sha256"] == "base"
    assert updated["outcome_contract_sha256"] == "outcome"
    assert updated["generation"] == 2
    assert updated["other"] == ["preserve", "me"]
    assert not path.with_name(path.name + ".tmp").exists()


def test_contract_axis_drift_is_detectable_separately_from_source_drift():
    declared = _declared()
    actual = {
        "source_sha256": "source-new",
        "base_prompt_sha256": "base-new",
        "outcome_contract_sha256": "outcome",
    }

    drift = finalize_change.identity_drift(declared, actual)
    contract_drift = {
        key: drift[key]
        for key in finalize_change.CONTRACT_KEYS
        if key in drift
    }

    assert contract_drift == {"base_prompt_sha256": ("base", "base-new")}


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


def test_main_refuses_contract_drift_without_writing(monkeypatch):
    declared = _valid_declared()
    actual = dict(declared)
    actual["source_sha256"] = "d" * 64
    actual["base_prompt_sha256"] = "e" * 64
    writes: list[tuple] = []

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)
    monkeypatch.setattr(finalize_change, "stamp_source", lambda *args: writes.append(args))

    assert finalize_change.main(["--write-source"]) == 2
    assert writes == []


def test_main_stamps_source_only_drift_once(monkeypatch):
    declared = _valid_declared()
    actual = dict(declared)
    actual["source_sha256"] = "d" * 64
    writes: list[tuple] = []

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)
    monkeypatch.setattr(finalize_change, "stamp_source", lambda *args: writes.append(args))

    assert finalize_change.main(["--write-source"]) == 0
    assert len(writes) == 1
    assert writes[0][1] is declared
    assert writes[0][2] == "d" * 64


def test_main_is_idempotent_when_declaration_matches(monkeypatch):
    declared = _valid_declared()
    writes: list[tuple] = []

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: dict(declared))
    monkeypatch.setattr(finalize_change, "stamp_source", lambda *args: writes.append(args))

    assert finalize_change.main(["--write-source"]) == 0
    assert writes == []


def test_main_fails_closed_when_identity_computation_is_unavailable(monkeypatch):
    declared = _valid_declared()
    writes: list[tuple] = []

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)

    def unavailable():
        raise OSError("tree disappeared")

    monkeypatch.setattr(finalize_change, "compute_identities", unavailable)
    monkeypatch.setattr(finalize_change, "stamp_source", lambda *args: writes.append(args))

    assert finalize_change.main(["--write-source"]) == 3
    assert writes == []


def test_main_fails_closed_when_source_stamp_cannot_be_written(monkeypatch):
    declared = _valid_declared()
    actual = dict(declared)
    actual["source_sha256"] = "d" * 64

    monkeypatch.setattr(finalize_change, "load_declaration", lambda: declared)
    monkeypatch.setattr(finalize_change, "compute_identities", lambda: actual)

    def fail_write(*_args):
        raise OSError("read only")

    monkeypatch.setattr(finalize_change, "stamp_source", fail_write)

    assert finalize_change.main(["--write-source"]) == 3
