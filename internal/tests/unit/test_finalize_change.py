from __future__ import annotations

import importlib.util
import json
from pathlib import Path


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
