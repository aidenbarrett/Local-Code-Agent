"""Regression tests for experiment provenance."""

from __future__ import annotations

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

    assert skill_files <= hashed, sorted(str(path.relative_to(REPO)) for path in skill_files - hashed)


def test_outcome_contract_hash_is_available_and_stable_shape():
    digest = provenance.outcome_contract_sha256()
    assert digest != "unavailable-no-source"
    assert len(digest) == 64
    int(digest, 16)


def test_outcome_contract_covers_the_task_contract_oracle_and_grader():
    source = provenance.outcome_contract_sha256()
    assert source != "unavailable-no-source"

    # Pin the three load-bearing surfaces rather than a particular digest. The
    # digest is declared in INSTRUMENT.json; this test protects the definition
    # of that digest from quietly shrinking later.
    run_case = provenance._function_source(REPO / "evaluation" / "run_evaluation.py", "run_case")
    assert run_case is not None
    assert "required_ok" in run_case
    assert "claim_ok" in run_case
    assert "scope_violation" in run_case
    assert "verification_disagreement" in run_case
    assert (REPO / "evaluation" / "task_contracts.py").is_file()
    assert (REPO / "evaluation" / "oracle.py").is_file()
