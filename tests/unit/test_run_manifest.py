from __future__ import annotations

import json
from pathlib import Path

import pytest

from measurement.capture_run_manifest import (
    _schema_hash,
    capture_manifest,
    model_architecture,
    schema_archive,
)


def test_schema_archive_stores_exact_schema_once_per_hash():
    archive, rows = schema_archive(["control", "narrow", "skill"])

    assert rows
    assert archive
    assert len(archive) < len(rows)  # repeated condition/case contracts deduplicate
    for row in rows:
        digest = row["tool_schema_hash"]
        assert digest in archive
        assert _schema_hash(archive[digest]) == digest


def test_narrow_and_skill_have_identical_tool_contract_per_case():
    _, rows = schema_archive(["narrow", "skill"])
    by_case = {}
    for row in rows:
        by_case.setdefault(row["case"], {})[row["condition"]] = row["tool_schema_hash"]

    assert by_case
    for case, conditions in by_case.items():
        assert conditions["narrow"] == conditions["skill"], case


def test_control_contract_is_distinct_from_narrow_on_current_fixture():
    _, rows = schema_archive(["control", "narrow"])
    by_case = {}
    for row in rows:
        by_case.setdefault(row["case"], {})[row["condition"]] = row["tool_schema_hash"]

    assert by_case
    assert all(v["control"] != v["narrow"] for v in by_case.values())


def test_model_architecture_does_not_turn_active_parameters_into_compute_claim():
    meta = model_architecture("qwen3-coder-30b")
    assert meta["architecture"] == "moe"
    assert meta["total_parameters_b"] == 30.5
    assert meta["active_parameters_b"] == 3.3
    assert "not a measurement" in meta["interpretation"]

    dense = model_architecture("qwen3-8b")
    assert dense["architecture"] == "dense"
    assert dense["parameter_label"] == "8B"


def test_unknown_model_metadata_fails_open_as_unknown_not_as_a_guess():
    meta = model_architecture("something-new")
    assert meta["architecture"] == "unknown"
    assert meta["metadata_quality"] == "unrecorded"


def test_manifest_marks_actual_device_unobserved_when_not_measured(monkeypatch):
    monkeypatch.delenv("LOCAL_AGENT_ACTUAL_DEVICE", raising=False)
    payload = capture_manifest("nuc-llama-8b", ["skill"], {"clean-build"})

    assert payload["target"]["requested_device"] == "CPU"
    assert payload["target"]["actual_device"] is None
    assert payload["target"]["actual_device_source"] == "unobserved"
    assert payload["measurement_quality"]["actual_device"] == "unobserved"
    assert payload["tool_schema_archive"]
    assert payload["instrument"]["declared"]["outcome_contract_sha256"]
    assert (
        payload["instrument"]["declared"]["outcome_contract_sha256"]
        == payload["instrument"]["observed"]["outcome_contract_sha256"]
    )


def test_operator_declared_actual_device_is_labelled_as_such(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_ACTUAL_DEVICE", "NPU.0")
    payload = capture_manifest("ptl-npu-8b", ["skill"], {"clean-build"})

    assert payload["target"]["actual_device"] == "NPU.0"
    assert payload["target"]["actual_device_source"] == "operator_declared"
