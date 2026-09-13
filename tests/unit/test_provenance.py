"""Regression tests for experiment provenance."""

from __future__ import annotations

import hashlib
from pathlib import Path

from local_agent import provenance


REPO = Path(__file__).resolve().parent.parent.parent


def test_source_provenance_includes_all_skill_files():
    """Every regular skill file can affect treatment and must move source_sha256."""
    hashed = {path.resolve() for path in provenance._files()}
    skill_files = {
        path.resolve()
        for path in (REPO / "skills").rglob("*")
        if path.is_file() and not provenance._SKIP.intersection(path.relative_to(REPO).parts)
    }

    assert skill_files <= hashed, sorted(
        path.relative_to(REPO).as_posix() for path in skill_files - hashed
    )


def test_outcome_contract_hash_is_available_and_stable_shape():
    digest = provenance.outcome_contract_sha256()
    assert digest != "unavailable-no-source"
    assert len(digest) == 64
    int(digest, 16)


def test_outcome_contract_covers_tasks_oracle_grader_and_endpoint_policy():
    source = provenance.outcome_contract_sha256()
    assert source != "unavailable-no-source"

    evaluator = REPO / "evaluation" / "run_evaluation.py"
    function_names = ("prepare", "establish", "_error_row", "run_case", "run_all")
    functions = {
        name: provenance._function_source(evaluator, name)
        for name in function_names
    }
    assert all(body is not None for body in functions.values())
    assert "required_ok" in functions["run_case"]
    assert "claim_ok" in functions["run_case"]
    assert "scope_violation" in functions["run_case"]
    assert "verification_disagreement" in functions["run_case"]
    assert "attempts_declared" in functions["run_all"]

    endpoints = (REPO / "evaluation" / "endpoints.py").read_bytes()
    assert b"ENGINEERING_CONTRACTS" in endpoints
    assert b"VALID_DRAWS_PER_TASK_CONDITION = 3" in endpoints
    assert b"MAX_ATTEMPTS_PER_TASK_CONDITION = 5" in endpoints

    parts: list[tuple[str, bytes]] = [
        ("evaluation/task_contracts.py", (REPO / "evaluation" / "task_contracts.py").read_bytes()),
        ("evaluation/oracle.py", (REPO / "evaluation" / "oracle.py").read_bytes()),
        ("evaluation/endpoints.py", endpoints),
    ]
    for name in function_names:
        body = functions[name]
        assert body is not None
        parts.append((f"evaluation.run_evaluation.{name}", body.encode("utf-8")))

    def digest(items):
        out = hashlib.sha256()
        for label, body in items:
            out.update(label.encode())
            out.update(b"\0")
            out.update(body)
            out.update(b"\0")
        return out.hexdigest()

    assert digest(parts) == source
    mutated = list(parts)
    mutated[2] = (mutated[2][0], mutated[2][1] + b"\n# endpoint-policy mutation\n")
    assert digest(mutated) != source
