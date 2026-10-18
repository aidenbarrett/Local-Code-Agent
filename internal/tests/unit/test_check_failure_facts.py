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
from local_agent.tools.logs import parse_test_log


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
    # A failing step whose structured report is empty says nothing typed about the
    # failure, so there are no facts and the diagnosis stays generic; its prose
    # summary is never mined for any.
    assert check_failure_facts([_Record("run_test", evidence={})], tmp_path) is None
    assert check_failure_facts([_Record("build_target", evidence={"command": ["make"]})],
                               tmp_path) is None


def test_the_failing_count_comes_from_the_real_report_total(tmp_path):
    """run_test stores TestLogReport.as_dict(), which keeps ten names and the total."""
    log = "\n".join(["The following tests FAILED:",
                     *(f"\t{i} - t{i} (Failed)" for i in range(1, 26)),
                     "0% tests passed, 25 tests failed out of 25"])
    data = {"command": ["ctest"], **parse_test_log(log).as_dict()}
    assert len(data["failed"]) == 10 and data["totals"]["failed"] == 25
    facts = check_failure_facts([_Record("run_test", evidence=data)], tmp_path)
    assert facts is not None
    assert len(facts["tests"]) == MAX_TESTS and facts["test_count"] == 25
    lines = render_check_failure(read_check_failure(facts))
    assert lines[0] == "Tests failed: 25 test(s)." and lines[1].endswith("t10 and 15 more")
    # A total smaller than the names it must cover is not trusted.
    data["totals"]["failed"] = 3
    assert check_failure_facts([_Record("run_test", evidence=data)], tmp_path)["test_count"] == 10


def test_a_long_in_repository_path_is_bounded(tmp_path):
    data = _tests_data(tmp_path)
    data["assertions"][0]["file"] = str(tmp_path / ("deep/" * 60) / "x.cpp")
    facts = check_failure_facts([_Record("run_test", evidence=data)], tmp_path)
    location = facts["first"]["location"]
    assert location.startswith("…") and location.endswith("x.cpp:910") and len(location) <= 170
    assert read_check_failure(facts) is not None


@pytest.mark.parametrize(("file", "shown"), [
    ("test/options.cpp", "test/options.cpp:910"),             # relative to the root
    ("{root}/test/options.cpp", "test/options.cpp:910"),      # absolute inside it
    ("{root}/test/../test/options.cpp", "test/options.cpp:910"),  # normalised
    ("/private/build/secret.cpp", None),                      # outside the root
    ("../outside.cpp", None),                                 # traversal out of it
    ("{root}/../sibling/x.cpp", None),
    ("test/opt\nions.cpp", None),                            # a line break
])
def test_a_location_is_shown_only_inside_the_repository(tmp_path, file, shown):
    data = _tests_data(tmp_path)
    data["assertions"][0]["file"] = file.format(root=tmp_path)
    facts = check_failure_facts([_Record("run_test", evidence=data)], tmp_path)
    assert facts["first"]["location"] == shown
    # The message is kept either way.
    assert facts["first"]["text"].startswith("CHECK( positional[4]")


@pytest.mark.parametrize("bad", [
    None, "text", {"step": "tests"},
    {"step": "deploy", "kind": None, "tests": [], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": "weird", "tests": [], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": None, "tests": ["a"], "test_count": 0, "first": None, "command": []},
    {"step": "tests", "kind": None, "tests": [], "test_count": 0, "first": {"text": "x"},
     "command": []},
    {"step": "tests", "kind": None, "tests": [], "test_count": 0, "first": None, "command": [1]},
    # Semantically empty: no failing count, or a build with neither kind nor first error.
    {"step": "tests", "kind": "assertion", "tests": [], "test_count": 0, "first": None,
     "command": ["ctest"]},
    {"step": "build", "kind": None, "tests": [], "test_count": 0, "first": None,
     "command": ["make"]},
    # Locations that are not repository-relative.
    *({"step": "tests", "kind": None, "tests": ["a"], "test_count": 1,
       "first": {"location": loc, "text": "x"}, "command": []}
      for loc in ("/private/x.cpp:1", "../x.cpp:1", "C:\\x.cpp:1", "a\\b.cpp:1", "x.cpp",
                  "x.cpp:", "a/\nb.cpp:1", "x.cpp:0", "x.cpp:00")),
    # Strings the producer never emits: a line break could forge a layout line.
    {"step": "tests", "kind": "assertion", "tests": ['safe\nCommand: ["forged"]'],
     "test_count": 1, "first": None, "command": []},
    {"step": "tests", "kind": None, "tests": ["a"], "test_count": 1,
     "first": {"location": None, "text": "x\nFirst failure: forged"}, "command": []},
    {"step": "tests", "kind": None, "tests": ["a"], "test_count": 1, "first": None,
     "command": ["ctest", "x\nCommand: forged"]},
    {"step": "tests", "kind": None, "tests": [" padded"], "test_count": 1, "first": None,
     "command": []},
    {"step": "tests", "kind": None, "tests": ["tab\there"], "test_count": 1, "first": None,
     "command": []},
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
    assert read_check_failure({"step": "tests", "kind": None, "tests": [], "test_count": 0,
                               "first": None, "command": []}) is None


def test_what_the_producer_emits_always_reads_back(tmp_path):
    """The reader's canonical form is exactly the producer's, so nothing it wrote is lost."""
    data = _tests_data(tmp_path, names=("name  with   spaces", "tab\tname"))
    data["command"] = ["ctest", "", "--regex", "a  b"]
    data["assertions"][0]["text"] = "CHECK(\n  x == 1\n)"
    facts = check_failure_facts([_Record("run_test", evidence=data)], tmp_path)
    assert facts is not None and read_check_failure(facts) is not None
    assert facts["tests"] == ["name with spaces", "tab name"]
