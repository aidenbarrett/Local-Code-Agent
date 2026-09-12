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

    # Pin the four load-bearing surfaces rather than a particular digest. The
    # digest is declared in INSTRUMENT.json; this test protects the definition
    # of that digest from quietly shrinking later.
    run_case = provenance._function_source(
        REPO / "evaluation" / "run_evaluation.py", "run_case"
    )
    run_all = provenance._function_source(
        REPO / "evaluation" / "run_evaluation.py", "run_all"
    )
    assert run_case is not None and run_all is not None
    assert "required_ok" in run_case
    assert "claim_ok" in run_case
    assert "scope_violation" in run_case
    assert "verification_disagreement" in run_case
    task_contracts = (REPO / "evaluation" / "task_contracts.py").read_text(encoding="utf-8")
    oracle = (REPO / "evaluation" / "oracle.py").read_text(encoding="utf-8")
    endpoints = (REPO / "evaluation" / "endpoints.py").read_text(encoding="utf-8")
    assert "ENGINEERING_CONTRACTS" in endpoints
    assert "VALID_DRAWS_PER_TASK_CONDITION = 3" in endpoints
    assert "MAX_ATTEMPTS_PER_TASK_CONDITION = 5" in endpoints

    parts = [
        ("evaluation/task_contracts.py", task_contracts),
        ("evaluation/oracle.py", oracle),
        ("evaluation/endpoints.py", endpoints),
        ("evaluation.run_evaluation.run_case", run_case),
        ("evaluation.run_evaluation.run_all", run_all),
    ]

    def digest(items):
        out = hashlib.sha256()
        for label, body in items:
            out.update(label.encode())
            out.update(b"\0")
            out.update(body.encode())
            out.update(b"\0")
        return out.hexdigest()

    assert digest(parts) == source
    mutated = list(parts)
    mutated[2] = (mutated[2][0], mutated[2][1] + "\n# endpoint-policy mutation\n")
    assert digest(mutated) != source
