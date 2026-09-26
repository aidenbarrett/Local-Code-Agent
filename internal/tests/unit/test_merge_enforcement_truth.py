from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


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


def test_active_ruleset_can_supply_required_status_checks():
    result = evaluate_merge_enforcement(
        {"protected": False},
        [
            {
                "enforcement": "active",
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
        ],
        expected_checks=("tests",),
    )
    assert result.enforced is True
    assert result.source == "ruleset"


def test_inactive_ruleset_does_not_count_as_enforcement():
    result = evaluate_merge_enforcement(
        {"protected": False},
        [
            {
                "enforcement": "disabled",
                "rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": [{"context": "tests"}]},
                    }
                ],
            }
        ],
    )
    assert result.enforced is False
