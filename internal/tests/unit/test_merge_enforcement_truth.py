from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import json
import urllib.error

import pytest


MODULE_PATH = Path(__file__).parents[2] / "devtools" / "check_merge_enforcement.py"
spec = spec_from_file_location("check_merge_enforcement", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
evaluate_merge_enforcement = module.evaluate_merge_enforcement


def test_unprotected_branch_without_ruleset_fails_closed():
    result = evaluate_merge_enforcement(
        {"protected": False, "protection": {"required_status_checks": {"contexts": [], "checks": []}}},
        [],
    )
    assert result.enforced is False
    assert result.source == "none"
    assert "unprotected" in result.reason


def test_classic_protection_requires_actual_status_checks():
    result = evaluate_merge_enforcement(
        {"protected": True, "protection": {"required_status_checks": {"contexts": [], "checks": []}}},
        [],
    )
    assert result.enforced is False
    assert result.source == "classic_branch_protection"


def test_classic_protection_reports_required_checks():
    result = evaluate_merge_enforcement(
        {
            "protected": True,
            "protection": {
                "required_status_checks": {
                    "contexts": ["tests", "product acceptance"],
                    "checks": [{"context": "serving", "app_id": 1}],
                }
            },
        },
        [],
        expected_checks=("tests", "serving"),
    )
    assert result.enforced is True
    assert result.required_checks == ("product acceptance", "serving", "tests")


def test_missing_expected_check_fails_closed():
    result = evaluate_merge_enforcement(
        {
            "protected": True,
            "protection": {"required_status_checks": {"contexts": ["tests"], "checks": []}},
        },
        [],
        expected_checks=("tests", "product acceptance"),
    )
    assert result.enforced is False
    assert "product acceptance" in result.reason


def _ruleset(enforcement: str) -> dict:
    return {
        "enforcement": enforcement,
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "tests"},
                        {"context": "product acceptance"},
                    ]
                },
            }
        ],
    }


def test_active_ruleset_can_supply_required_status_checks():
    result = evaluate_merge_enforcement(
        {"protected": False},
        _ruleset("active")["rules"],
        expected_checks=("tests",),
    )
    assert result.enforced is True
    assert result.source == "ruleset"


@pytest.mark.parametrize("enforcement", ["active", "disabled", "evaluate"])
def test_repository_wide_ruleset_metadata_is_not_branch_evidence(enforcement):
    result = evaluate_merge_enforcement(
        {"protected": False},
        [_ruleset(enforcement)],
    )
    assert result.enforced is False


def test_an_applicable_rule_without_required_checks_does_not_certify():
    result = evaluate_merge_enforcement(
        {"protected": False},
        [{"type": "required_status_checks", "parameters": {"required_status_checks": []}}],
    )
    assert result.enforced is False


def test_ruleset_excluding_main_cannot_certify_main(monkeypatch, capsys):
    unrelated = {**_ruleset("active"), "target": "branch", "conditions": {
        "ref_name": {"include": ["refs/heads/release"], "exclude": ["refs/heads/main"]},
    }}
    urls = []

    def request(url, token):
        urls.append(url)
        if "/branches/main" in url and "/rules/" not in url:
            return {"protected": False}
        if "/rules/branches/main?" in url:
            return []  # GitHub excludes the unrelated active ruleset.
        if url.endswith("/rulesets"):
            return [unrelated]
        raise AssertionError(url)

    monkeypatch.setattr(module, "_request_json", request)
    assert module.main(["--repository", "owner/repo"]) == 2
    assert json.loads(capsys.readouterr().out)["enforced"] is False
    assert not any(url.endswith("/rulesets") for url in urls)


def test_branch_rules_paginate_and_encode_the_requested_branch(monkeypatch):
    urls = []

    def request(url, token):
        urls.append(url)
        assert token == "test-token"
        if url.endswith("page=1"):
            return [{"type": "deletion"}] * 100
        return _ruleset("active")["rules"]

    monkeypatch.setattr(module, "_request_json", request)
    rules = module._load_branch_rules("owner", "repo", "release/next", "test-token")
    assert len(urls) == 2
    assert all("/rules/branches/release%2Fnext?per_page=100&page=" in url for url in urls)
    assert evaluate_merge_enforcement({"protected": False}, rules).enforced


@pytest.mark.parametrize("payload", [{}, [None]])
def test_malformed_branch_rules_refuse(monkeypatch, payload):
    monkeypatch.setattr(module, "_request_json", lambda *_: payload)
    with pytest.raises(ValueError, match="malformed"):
        module._load_branch_rules("owner", "repo", "main", None)


def test_branch_rules_pagination_is_bounded(monkeypatch):
    monkeypatch.setattr(module, "_request_json", lambda *_: [{"type": "deletion"}] * 100)
    with pytest.raises(ValueError, match="observation limit"):
        module._load_branch_rules("owner", "repo", "main", None)


def test_unavailable_effective_rules_never_fall_back_to_global_rulesets(monkeypatch, capsys):
    def request(url, token):
        if "/rules/" in url:
            raise urllib.error.URLError("unavailable")
        return {"protected": False}

    monkeypatch.setattr(module, "_request_json", request)
    assert module.main(["--repository", "owner/repo"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "unknown" and result["enforced"] is False
