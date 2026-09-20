from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "devtools" / "check_change.py"
SPEC = importlib.util.spec_from_file_location("lca_check_change", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
check_change = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_change)


def test_contract_drift_is_empty_when_frozen_axes_match():
    trusted = {
        "source_sha256": "old",
        "base_prompt_sha256": "base",
        "outcome_contract_sha256": "outcome",
    }
    candidate = {
        "source_sha256": "new",
        "base_prompt_sha256": "base",
        "outcome_contract_sha256": "outcome",
    }
    assert check_change.contract_drift(trusted, candidate) == {}


def test_contract_drift_reports_each_frozen_axis():
    trusted = {"base_prompt_sha256": "a", "outcome_contract_sha256": "b"}
    candidate = {"base_prompt_sha256": "x", "outcome_contract_sha256": "y"}
    assert check_change.contract_drift(trusted, candidate) == {
        "base_prompt_sha256": ("a", "x"),
        "outcome_contract_sha256": ("b", "y"),
    }


def test_main_fails_closed_when_candidate_is_not_descendant(monkeypatch):
    monkeypatch.setattr(check_change, "require_clean", lambda: None)
    monkeypatch.setattr(
        check_change, "resolve", lambda ref: "a" * 40 if ref == "main" else "b" * 40
    )
    monkeypatch.setattr(check_change, "is_ancestor", lambda *_args: False)
    monkeypatch.setattr(
        check_change,
        "read_json_at",
        lambda *_args: pytest.fail("must not inspect policy after ancestry failure"),
    )
    assert check_change.main(["--base", "main"]) == 2


def test_main_fails_closed_on_contract_axis_drift(monkeypatch):
    monkeypatch.setattr(check_change, "require_clean", lambda: None)
    monkeypatch.setattr(
        check_change, "resolve", lambda ref: "a" * 40 if ref == "main" else "b" * 40
    )
    monkeypatch.setattr(check_change, "is_ancestor", lambda *_args: True)
    declarations = iter([
        {"base_prompt_sha256": "base", "outcome_contract_sha256": "outcome"},
        {"base_prompt_sha256": "changed", "outcome_contract_sha256": "outcome"},
    ])
    monkeypatch.setattr(check_change, "read_json_at", lambda *_args: next(declarations))
    assert check_change.main(["--base", "main"]) == 2


def test_main_accepts_source_only_identity_move(monkeypatch):
    monkeypatch.setattr(check_change, "require_clean", lambda: None)
    monkeypatch.setattr(
        check_change, "resolve", lambda ref: "a" * 40 if ref == "main" else "b" * 40
    )
    monkeypatch.setattr(check_change, "is_ancestor", lambda *_args: True)
    declarations = iter([
        {
            "source_sha256": "old",
            "base_prompt_sha256": "base",
            "outcome_contract_sha256": "outcome",
        },
        {
            "source_sha256": "new",
            "base_prompt_sha256": "base",
            "outcome_contract_sha256": "outcome",
        },
    ])
    monkeypatch.setattr(check_change, "read_json_at", lambda *_args: next(declarations))
    assert check_change.main(["--base", "main"]) == 0
