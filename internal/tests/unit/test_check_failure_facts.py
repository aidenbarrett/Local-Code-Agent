"""A failed configured check keeps typed, bounded facts for "why did that fail?".

The facts come only from the failing step's structured tool report (never its prose),
are repository-relative and bounded, and a missing or malformed block leaves the
diagnosis generic rather than inventing precision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from local_agent.session.candidate_facts import (
    RESULT_FIELDS, RESULT_FIELDS_V1, RESULT_FIELDS_V2, RESULT_SCHEMA, result_fields_for,
)
from local_agent.session.check_failure import (
    MAX_TESTS, CheckFailure, FirstFailure, check_failure_facts, project_check_failure,
    read_check_failure, render_check_failure,
)
from local_agent.session.conversation_gateway import ConversationGateway
from local_agent.session.intents import RULE_TASK_DIAGNOSTIC, RouteAction, RouteDecision, RouteSource
from local_agent.session.task_history import TaskObservation


@dataclass
class _Record:
    name: str
    execution: str = "ok"
    domain: str = "fail"
    summary: str = "prose that must never be parsed: at fake.cpp:1"
    evidence: dict[str, Any] = field(default_factory=dict)


def _tests_data(root: Path, *, names=("options", "options_no_regex")) -> dict[str, Any]:
    return {
        "command": ["ctest", "--test-dir", "build", "-C", "Debug"],
        "failure_kind": "assertion",
        "failed": [{"name": n, "status": "Failed", "log_line": 3} for n in names],
        "assertions": [{"file": str(root / "test" / "options.cpp"), "line": 910, "log_line": 5,
                        "text": "CHECK( positional[4] == 0xab ) with expansion: 154 == 171"}],
        "crash_markers": [],
    }


def _build_data(root: Path) -> dict[str, Any]:
    return {
        "command": ["cmake", "--build", "build"],
        "failure_kind": "compile",
        "errors": [{"file": str(root / "src" / "ring.cpp"), "line": 13, "severity": "error",
                    "message": "'count' was not declared in this scope", "log_line": 7}],
        "link_errors": [], "cmake_errors": [],
    }


def test_a_failed_test_step_projects_relative_bounded_facts(tmp_path):
    history = [_Record("build_target", domain="pass", evidence=_build_data(tmp_path)),
               _Record("run_test", evidence=_tests_data(tmp_path))]
    facts = check_failure_facts(history, tmp_path)
    assert facts == {
        "step": "tests", "kind": "assertion", "tests": ["options", "options_no_regex"],
        "test_count": 2, "command": ["ctest", "--test-dir", "build", "-C", "Debug"],
        "first": {"location": "test/options.cpp:910",
                  "text": "CHECK( positional[4] == 0xab ) with expansion: 154 == 171"},
    }
    assert str(tmp_path) not in str(facts)


def test_a_failed_build_step_names_its_first_error(tmp_path):
    facts = check_failure_facts([_Record("build_target", evidence=_build_data(tmp_path))], tmp_path)
    assert facts is not None and facts["step"] == "build" and facts["kind"] == "compile"
    assert facts["first"] == {"location": "src/ring.cpp:13",
                              "text": "'count' was not declared in this scope"}
    assert facts["tests"] == [] and facts["test_count"] == 0


def test_only_an_executed_failing_step_with_typed_data_counts(tmp_path):
    assert check_failure_facts([_Record("run_test", domain="pass")], tmp_path) is None
    assert check_failure_facts([_Record("run_test", execution="error")], tmp_path) is None
    assert check_failure_facts([_Record("read_file")], tmp_path) is None
    # A failing step whose structured report is empty yields facts without a first
    # failure; its prose summary is never mined for one.
    facts = check_failure_facts([_Record("run_test", evidence={})], tmp_path)
    assert facts is not None and facts["first"] is None and facts["kind"] is None


def test_names_and_paths_are_bounded(tmp_path):
    names = tuple(f"t{i}" for i in range(25))
    data = _tests_data(tmp_path, names=names)
    data["assertions"][0]["file"] = "/outside/" + "deep/" * 60 + "x.cpp"
    facts = check_failure_facts([_Record("run_test", evidence=data)], tmp_path)
    assert facts is not None
    assert len(facts["tests"]) == MAX_TESTS and facts["test_count"] == 25
    location = facts["first"]["location"]
    assert location.startswith("…") and location.endswith("x.cpp:910") and len(location) <= 170
    assert read_check_failure(facts) is not None


@pytest.mark.parametrize("bad", [
    None, "text", {"step": "tests"},
    {"step": "deploy", "kind": None, "tests": [], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": "weird", "tests": [], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": None, "tests": ["a"], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": None, "tests": [], "test_count": 0, "first": {"text": "x"},
     "command": []},
    {"step": "tests", "kind": None, "tests": [], "test_count": 0, "first": None, "command": [1]},
])
def test_malformed_facts_read_as_absent(bad):
    assert read_check_failure(bad) is None
    assert project_check_failure({"check_failure": bad}) is None


def test_result_schema_three_adds_only_check_failure():
    assert RESULT_SCHEMA == "lca.task-result/3"
    assert result_fields_for("lca.task-result/3") == RESULT_FIELDS == RESULT_FIELDS_V2 | {
        "check_failure"}
    assert result_fields_for("lca.task-result/2") == RESULT_FIELDS_V2
    assert result_fields_for("lca.task-result/1") == RESULT_FIELDS_V1
    with pytest.raises(ValueError):
        result_fields_for("lca.task-result/4")


def _decision(task_id: str) -> RouteDecision:
    return RouteDecision(RouteAction.WORK, source=RouteSource.RULE, skill="task-diagnostic",
                         objective="why did that fail?", rule_id=RULE_TASK_DIAGNOSTIC,
                         reference_ids=(task_id,))


def _diagnosis(check: str, facts: CheckFailure | None) -> str:
    task_id = "11111111-1111-4111-8111-111111111111"
    observation = TaskObservation(task_id=task_id, turn_ref={}, status="failed",
                                  verdict="FAILED", answer="RECORDED PROSE",
                                  evidence_ids=("run_test:1",), configured_check=check,
                                  check_failure=facts)
    answer = ConversationGateway._recorded_check_diagnosis(_decision(task_id), {task_id: observation})
    assert answer is not None
    return answer


def test_a_test_failure_diagnosis_has_a_fixed_layout():
    facts = CheckFailure("tests", "assertion", ("options", "options_no_regex"), 2,
                         FirstFailure("test/options.cpp:910", "CHECK( x ) with expansion: 1 == 2"),
                         ("ctest", "--test-dir", "build"))
    answer = _diagnosis("run-tests", facts)
    assert answer.startswith(
        "Task 11111111-1111-4111-8111-111111111111 (run the tests) failed.\n"
        "Tests failed (assertion): 2 test(s).\n"
        "Failing tests: options, options_no_regex\n"
        "First failure: test/options.cpp:910\n"
        "  CHECK( x ) with expansion: 1 == 2\n"
        'Command: ["ctest", "--test-dir", "build"]\n\n'
        "This is the result recorded when that check ran")
    assert "RECORDED PROSE" not in answer


def test_a_build_failure_diagnosis_has_a_fixed_layout():
    facts = CheckFailure("build", "link", (), 0, FirstFailure(None, "undefined reference to `f()'"),
                         ("cmake", "--build", "build"))
    answer = _diagnosis("run-build", facts)
    assert answer.startswith(
        "Task 11111111-1111-4111-8111-111111111111 (build it) failed.\n"
        "The build failed (link).\n"
        "First error:\n"
        "  undefined reference to `f()'\n"
        'Command: ["cmake", "--build", "build"]\n\n')


def test_without_typed_facts_the_diagnosis_stays_generic():
    answer = _diagnosis("run-tests", None)
    assert answer.startswith("Task 11111111-1111-4111-8111-111111111111 (run the tests) failed. "
                             "Its recorded result:\nRECORDED PROSE\n\n")
    assert render_check_failure(CheckFailure("tests", None, (), 0, None, ())) == [
        "Tests failed: 0 test(s)."]
