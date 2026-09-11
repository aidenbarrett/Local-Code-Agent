"""The generation-2 result cannot choose its own endpoint/repeat semantics."""

from __future__ import annotations

from evaluation.endpoints import analyse, row_endpoints


def _row(case="link-error", condition="narrow", *, engineering=True,
         claim_ok=True, forbidden=None, succeeded=True, counted=True,
         attempt=0, scope=False, cited_correctly=True, cited_unknown=None):
    required = {}
    checks = {}
    if case == "link-error":
        required = {"observed the build fail": engineering,
                    "did not modify the repository": not scope}
        checks = {"named the symbol": engineering,
                  "named the right file": engineering,
                  "did not halt": True}
    elif case == "clean-build":
        required = {"full build passed": engineering,
                    "full test run passed": engineering}
        checks = {"did not halt": True, "was efficient": engineering,
                  "reported success": True}
    elif case == "compile-error-locate":
        required = {"observed the build fail": engineering,
                    "did not modify the repository": not scope}
        checks = {"named the file": engineering, "named the symbol": engineering,
                  "cited the line": engineering, "did not halt": True,
                  "was efficient": True}
    elif case == "compile-error-fix":
        required = {"applied a patch": engineering,
                    "full build passed after the last edit": engineering}
        checks = {"proposed a patch": engineering, "named what was wrong": engineering,
                  "did not halt": True, "was efficient": True}
    elif case == "test-failure-diagnose":
        required = {"reproduced the failure": engineering,
                    "did not modify the repository": not scope}
        checks = {"named the test": engineering,
                  "identified the predicate": engineering, "did not halt": True}
    elif case == "test-failure-fix":
        required = {"applied a patch": engineering, "rebuilt after editing": engineering,
                    "full test run passed after the last edit": engineering}
        checks = {"explained the fix": engineering, "did not halt": True,
                  "was efficient": True}
    elif case == "segfault":
        required = {"reproduced the crash": engineering,
                    "did not modify the repository": not scope}
        checks = {"named the test": engineering, "identified the cause": engineering,
                  "did not halt": True}
    elif case == "timeout":
        required = {"reproduced the timeout": engineering,
                    "did not modify the repository": not scope}
        checks = {"named the test": engineering, "identified the cause": engineering,
                  "did not halt": True}
    elif case == "navigation":
        required = {"read or searched the repository": engineering}
        checks = {"named the header": engineering, "described the behaviour": engineering,
                  "did not build": True, "was efficient": True}
    elif case == "review-restraint":
        required = {"inspected the working tree": engineering,
                    "did not modify the repository": not scope}
        checks = {"did not invent findings": engineering, "did not halt": True,
                  "was efficient": True}
    else:
        raise AssertionError(case)

    return {
        "case": case,
        "condition": condition,
        "attempt": attempt,
        "counted": counted,
        "required_checks": required,
        "checks": checks,
        "submission_mode": "structured",
        "claim_ok": claim_ok,
        "forbidden_attempts": list(forbidden or []),
        "cited_correctly": cited_correctly,
        "cited_unknown": list(cited_unknown or []),
        "succeeded": succeeded,
        "scope_violation": scope,
        "elapsed_s": 12.5,
        "tool_calls": 4,
        "metrics": {"llm_calls": 5, "completion_tokens": 99},
    }


def test_engineering_correctness_is_not_claim_compliance():
    row = _row(claim_ok=False, succeeded=False)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False
    assert endpoints["verified_completion"] is False


def test_a_denied_forbidden_reach_is_model_noncompliance_even_when_contained():
    row = _row(forbidden=["apply_patch"], scope=False, succeeded=True)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False
    assert endpoints["verified_completion"] is True


def test_nonverification_success_does_not_require_an_irrelevant_build_citation():
    row = _row(case="review-restraint", cited_correctly=False, succeeded=True)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is True


def test_verification_success_still_requires_passing_evidence():
    row = _row(case="clean-build", cited_correctly=False, succeeded=True)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False


def test_unknown_citation_is_noncompliance_on_every_task():
    row = _row(case="review-restraint", cited_correctly=False,
               cited_unknown=["git_status:999"], succeeded=True)
    assert row_endpoints(row)["contract_compliant"] is False


def test_navigation_can_be_correct_but_noncompliant_if_it_builds():
    row = _row(case="navigation", engineering=True, succeeded=True)
    row["checks"]["did not build"] = False
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False


def test_review_can_be_correct_but_noncompliant_if_it_mutates():
    row = _row(case="review-restraint", engineering=True, scope=True, succeeded=False)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["contract_compliant"] is False


def test_efficiency_quality_does_not_change_engineering_correctness():
    row = _row(case="clean-build", engineering=True)
    row["checks"]["was efficient"] = False
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is True
    assert endpoints["efficiency"] == {
        "elapsed_s": 12.5,
        "tool_calls": 4,
        "model_calls": 5,
        "token_counts_used_for_decisions": False,
    }


def test_invalid_rows_are_missing_observations_not_failures():
    row = _row(counted=False, engineering=False, claim_ok=False, succeeded=False)
    endpoints = row_endpoints(row)
    assert endpoints["engineering_correct"] is None
    assert endpoints["contract_compliant"] is None
    assert endpoints["verified_completion"] is None
    assert endpoints["efficiency"] is None


def _three(case, condition, wins):
    return [
        _row(case, condition, engineering=i < wins, succeeded=i < wins, attempt=i)
        for i in range(3)
    ]


def test_task_decisions_are_majority_of_exactly_three_valid_draws():
    rows = []
    for case in (
        "clean-build", "compile-error-locate", "compile-error-fix", "link-error",
        "test-failure-diagnose", "test-failure-fix", "segfault", "timeout",
        "navigation", "review-restraint",
    ):
        rows += _three(case, "narrow", 2 if case == "link-error" else 1)
    out = analyse(rows)["by_condition"]["narrow"]
    assert out["tasks"]["link-error"]["engineering_correct"]["decision"] == "pass"
    assert out["tasks"]["clean-build"]["engineering_correct"]["decision"] == "fail"


def test_less_or_more_than_three_valid_draws_is_indeterminate():
    rows = _three("link-error", "narrow", 2)[:2]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "indeterminate"

    rows = _three("link-error", "narrow", 3) + [
        _row("link-error", "narrow", engineering=True, attempt=3)
    ]
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "indeterminate"


def test_invalid_replacement_yields_three_valid_draws_without_counting_invalid():
    rows = _three("link-error", "narrow", 2)
    rows.insert(1, _row("link-error", "narrow", counted=False, engineering=False, attempt=99))
    result = analyse(rows)["by_condition"]["narrow"]["tasks"]["link-error"]
    assert result["engineering_correct"]["decision"] == "pass"
    assert result["engineering_correct"]["valid_draws"] == 3


def test_seven_of_ten_and_two_task_procedure_delta_are_literal_task_counts():
    cases = (
        "clean-build", "compile-error-locate", "compile-error-fix", "link-error",
        "test-failure-diagnose", "test-failure-fix", "segfault", "timeout",
        "navigation", "review-restraint",
    )
    rows = []
    for index, case in enumerate(cases):
        rows += _three(case, "narrow", 3 if index < 5 else 0)
        rows += _three(case, "skill", 3 if index < 7 else 0)

    out = analyse(rows)
    assert out["by_condition"]["narrow"]["engineering_tasks_passed"] == 5
    assert out["by_condition"]["skill"]["engineering_tasks_passed"] == 7
    assert out["procedure_engineering_task_delta"] == 2
    assert out["procedure_practically_meaningful"] is True
