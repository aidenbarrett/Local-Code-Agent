"""A failed build or test names its kind, its first evidence and the failing argv (#462).

The deterministic parsers already found compile, link and CMake errors, assertions,
crashes and ctest timeouts; nothing reduced them to a kind, the build summary named
only a compile error's location, and neither summary carried the command.
"""
from __future__ import annotations

import json
import shutil

import pytest

from local_agent.tools.logs import parse_build_log, parse_test_log

needs_cmake = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None,
    reason="the C++ fixture needs cmake and ctest",
)


@pytest.mark.parametrize(("log", "kind"), [
    ("src/a.cpp:3:5: error: expected ';'\nundefined reference to `f()'\n", "compile"),
    (r"C:\src\a.cpp(3,5): error C2143: syntax error" + "\n", "compile"),
    ("main.o: in function `main':\nmain.cpp:(.text+0x5): undefined reference to `f()'\n", "link"),
    ("main.obj : error LNK2019: unresolved external symbol \"int f(void)\" referenced in function main\n",
     "link"),
    ("CMake Error at CMakeLists.txt:3 (add_executable):\n  Cannot find source file\n", "configure"),
    ("CMake Deprecation Warning at CMakeLists.txt:1\n", None),
    ("make: *** [all] Error 2\n", None),
])
def test_a_build_log_is_reduced_to_one_kind(log, kind):
    report = parse_build_log(log)
    assert report.failure_kind == kind
    assert report.as_dict()["failure_kind"] == kind


@pytest.mark.parametrize(("log", "kind"), [
    ("  1 - order (Failed)\n", "failed"),
    ("  1 - slow (Timeout)\n", "timeout"),
    ("  1 - boom (SEGFAULT)\n", "crash"),
    ("  1 - boom (Subprocess aborted)\nterminate called after throwing an instance of 'x'\n", "crash"),
    ("t: a.cpp:9: int main(): Assertion `x == 1' failed.\n  1 - t (Subprocess aborted)\n", "assertion"),
    ("100% tests passed, 0 tests failed out of 3\n", None),
])
def test_a_test_log_is_reduced_to_one_kind(log, kind):
    report = parse_test_log(log)
    assert report.failure_kind == kind
    assert report.as_dict()["failure_kind"] == kind


def _registry(sandbox, scenario: str):
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    sandbox.scenario(scenario)
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    return registry


@needs_cmake
@pytest.mark.parametrize(("scenario", "kind", "evidence"), [
    ("compile_error", "compile", "first error at "),
    ("link_error", "link", "first unresolved symbol: "),
])
def test_a_failed_build_names_kind_evidence_and_command(sandbox, scenario, kind, evidence):
    result = _registry(sandbox, scenario).get("build_target").handler()
    assert result.ok is False
    assert result.data["failure_kind"] == kind
    assert f"FAILED ({kind})" in result.summary
    assert evidence in result.summary
    assert f" -- command: {json.dumps(result.data['command'])}" in result.summary


@needs_cmake
@pytest.mark.parametrize(("scenario", "kind", "evidence"), [
    # The fixture's tests print "file:line: Assertion <expr> failed." and exit 1.
    ("test_failure", "assertion", "first assertion evidence: "),
    ("crash", "crash", "FAILED (crash)"),
])
def test_a_failed_test_run_names_kind_and_command(sandbox, scenario, kind, evidence):
    registry = _registry(sandbox, scenario)
    assert registry.get("build_target").handler().ok
    result = registry.get("run_test").handler()
    assert result.ok is False
    assert result.data["failure_kind"] == kind
    assert f"FAILED ({kind})" in result.summary
    assert evidence in result.summary
    assert f" -- command: {json.dumps(result.data['command'])}" in result.summary


@needs_cmake
def test_a_passing_build_and_test_run_carry_no_failure_kind(sandbox):
    registry = _registry(sandbox, "clean")
    built = registry.get("build_target").handler()
    tested = registry.get("run_test").handler()
    assert built.ok and tested.ok
    assert built.data["failure_kind"] is None and tested.data["failure_kind"] is None
    assert "command:" not in built.summary and "command:" not in tested.summary
