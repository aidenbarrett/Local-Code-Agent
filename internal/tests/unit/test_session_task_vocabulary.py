"""Keep Session Hub task-domain names explicit without moving their wire values."""
from __future__ import annotations

import ast
from pathlib import Path

from local_agent.session import contracts
from local_agent.session.contracts import (
    TASK_OUTCOME_PROJECTIONS,
    TaskOutcome,
    TaskOutcomeProjection,
    TaskVerdict,
)


INTERNAL = Path(__file__).resolve().parents[2]
SESSION = INTERNAL / "local_agent" / "session"
ACTIVE_ROOTS = (
    INTERNAL / "local_agent",
    INTERNAL / "scripts",
    INTERNAL / "tests",
)


def test_task_domain_type_names_are_explicit_and_legacy_aliases_do_not_return():
    assert TaskOutcome.__name__ == "TaskOutcome"
    assert TaskVerdict.__name__ == "TaskVerdict"
    assert TaskOutcomeProjection.__name__ == "TaskOutcomeProjection"

    legacy = {
        "Product" + "Outcome",
        "Out" + "comeProjection",
        "OUTCOME" + "_PROJECTIONS",
    }
    assert not [name for name in legacy if hasattr(contracts, name)]
    assert not hasattr(contracts, "Ver" + "dict")


def test_task_outcome_and_verdict_wire_values_are_unchanged_by_the_python_rename():
    assert {item.name: item.value for item in TaskOutcome} == {
        "PASS": "pass",
        "ESCALATED_PASS": "escalated_pass",
        "ESCALATED_FAIL": "escalated_fail",
        "FAIL": "fail",
        "BLOCKED": "blocked",
        "NO_VERDICT": "no_verdict",
    }
    assert {item.name: item.value for item in TaskVerdict} == {
        "VERIFIED": "VERIFIED",
        "FAILED": "FAILED",
        "REFUSED": "REFUSED",
        "NO_VERDICT": "NO_VERDICT",
        "NOT_REQUIRED": "NOT_REQUIRED",
    }
    assert set(TASK_OUTCOME_PROJECTIONS) == set(TaskOutcome)
    assert all(isinstance(value, TaskOutcomeProjection) for value in TASK_OUTCOME_PROJECTIONS.values())


def _active_python() -> list[Path]:
    paths: list[Path] = []
    for root in ACTIVE_ROOTS:
        paths.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(set(paths))


def test_active_code_does_not_reintroduce_ambiguous_session_task_type_names():
    old_outcome = "Product" + "Outcome"
    old_projection = "Out" + "comeProjection"
    old_projection_map = "OUTCOME" + "_PROJECTIONS"
    old_verdict = "Ver" + "dict"
    legacy_outcome_names = {old_outcome, old_projection, old_projection_map}
    offenders: list[str] = []

    for path in _active_python():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(INTERNAL).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in legacy_outcome_names:
                offenders.append(f"{relative}:{node.lineno}:{node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in legacy_outcome_names:
                offenders.append(f"{relative}:{node.lineno}:{node.attr}")
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("session.contracts"):
                for alias in node.names:
                    if alias.name == old_verdict or alias.name in legacy_outcome_names:
                        offenders.append(f"{relative}:{node.lineno}:{alias.name}")

        if path.is_relative_to(SESSION):
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == old_verdict:
                    offenders.append(f"{relative}:{node.lineno}:{old_verdict}")
                elif isinstance(node, ast.Attribute) and node.attr == old_verdict:
                    offenders.append(f"{relative}:{node.lineno}:{old_verdict}")

    assert not offenders, "ambiguous Session task vocabulary returned:\n  " + "\n  ".join(sorted(set(offenders)))
