"""Freeze semantic provenance labels across source and file renames.

The files and Python symbols feeding the provenance hashes may move. Their
semantic labels must not move accidentally with them: those labels are bytes in
the model-facing and outcome-facing generation axes.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

PROVENANCE = INTERNAL / "local_agent" / "provenance.py"

FROZEN_BASE_PROMPT_LABELS = {
    "SYSTEM_PROMPT",
    "context.build_system_message",
    "context.build_skill_message",
    "context.tool_result_message",
    "contracts._execute",
    "contracts._accept_answer",
}

FROZEN_OUTCOME_LABELS = {
    "evaluation/task_contracts.py",
    "evaluation/oracle.py",
    "evaluation/endpoints.py",
}
FROZEN_OUTCOME_LABEL_PREFIX = "evaluation.run_evaluation."
FROZEN_EVALUATOR_FUNCTIONS = {
    "prepare", "establish", "_error_row", "run_case", "run_all",
}


def _string_literals(function_name: str) -> set[str]:
    tree = ast.parse(PROVENANCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return {
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
    raise AssertionError(f"{function_name} not found in provenance.py")


def test_base_prompt_labels_are_unchanged():
    literals = _string_literals("base_prompt_sha256")
    missing = sorted(FROZEN_BASE_PROMPT_LABELS - literals)
    assert not missing, (
        f"base_prompt_sha256 lost frozen semantic labels: {missing}. Rename paths "
        "and symbols without renaming the labels unless the generation axis is "
        "deliberately changing."
    )


def test_outcome_contract_labels_are_unchanged():
    literals = _string_literals("outcome_contract_sha256")
    missing = sorted(FROZEN_OUTCOME_LABELS - literals)
    assert not missing, f"outcome_contract_sha256 lost frozen semantic labels: {missing}"
    assert FROZEN_OUTCOME_LABEL_PREFIX in literals, (
        "the evaluator per-function label prefix changed; that moves the outcome contract identity"
    )
    missing_functions = sorted(FROZEN_EVALUATOR_FUNCTIONS - literals)
    assert not missing_functions, (
        f"outcome_contract_sha256 no longer selects evaluator functions: {missing_functions}"
    )


def test_outcome_contract_sources_still_resolve():
    from local_agent import provenance

    value = provenance.outcome_contract_sha256()
    assert value != "unavailable-no-source", (
        "an outcome-contract source path or selected evaluator function no longer resolves; "
        "update physical paths without changing frozen semantic labels"
    )
    assert len(value) == 64


def test_model_facing_sources_still_resolve():
    from local_agent import provenance

    value = provenance.base_prompt_sha256()
    assert value != "unavailable-no-source", (
        "a model-facing source path or symbol no longer resolves; update physical references "
        "without changing frozen semantic labels"
    )
    assert len(value) == 64
