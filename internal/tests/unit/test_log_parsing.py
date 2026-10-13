from __future__ import annotations

import json

import pytest

from local_agent.agent.context import _collapse, tool_result_message
from local_agent.tools.logs import parse_build_log, parse_test_log
from local_agent.tools.testing_tools import _failed_tests_summary

GCC = """
[ 20%] Building CXX object CMakeFiles/sandbox.dir/src/ring_buffer.cpp.o
/repo/src/ring_buffer.cpp:13:7: error: 'count' was not declared in this scope; did you mean 'count_'?
   13 |     ++count;
      |       ^~~~~
/repo/src/ring_buffer.cpp:27:52: error: expected ';' before '}' token
/repo/src/text_util.cpp:28:23: warning: comparison of integer expressions of different signedness [-Wsign-compare]
make[2]: *** [CMakeFiles/sandbox.dir/build.make:76: ring_buffer.cpp.o] Error 1
"""

MSVC = r"""
C:\repo\src\ring_buffer.cpp(13,7): error C2065: 'count': undeclared identifier
C:\repo\src\text_util.cpp(28,23): warning C4018: '<': signed/unsigned mismatch
"""

LINK = """
/usr/bin/ld: CMakeFiles/test_ring_buffer.dir/tests/test_ring_buffer.cpp.o: in function `main':
test_ring_buffer.cpp:(.text+0x3c1): undefined reference to `sandbox::checksum(std::vector<int> const&)'
collect2: error: ld returned 1 exit status
"""

MSVC_LINK = r"""
test_ring_buffer.obj : error LNK2019: unresolved external symbol "int __cdecl sandbox::checksum(class std::vector<int,class std::allocator<int> > const &)" (?checksum@sandbox@@YAHAEBV?$vector@HV?$allocator@H@std@@@std@@@Z) referenced in function main
"""

CTEST_FAIL = """
    Start 1: ring_buffer
1/4 Test #1: ring_buffer ......................***Exception: SubprocessAborted  0.00 sec
test_ring_buffer: tests/test_ring_buffer.cpp:16: int main(): Assertion `buffer.pop().value() == 1' failed.
    Start 3: fd_owner
3/4 Test #3: fd_owner .........................   Passed    0.00 sec

75% tests passed, 1 tests failed out of 4

The following tests FAILED:
	  1 - ring_buffer (Subprocess aborted)
"""

CTEST_TIMEOUT = """
4/4 Test #4: slow .............................***Timeout   5.01 sec

75% tests passed, 1 tests failed out of 4

The following tests FAILED:
	  4 - slow (Timeout)
"""

CTEST_SEGV = """
2/4 Test #2: text_util ........................***Exception: SegFault  0.00 sec
Segmentation fault (core dumped)

The following tests FAILED:
	  2 - text_util (SEGFAULT)
"""


def test_gcc_diagnostics():
    report = parse_build_log(GCC)
    assert len(report.errors) == 2
    first = report.errors[0]
    assert first.file.endswith("ring_buffer.cpp")
    assert first.line == 13
    assert "count" in first.message
    assert first.log_line == 3
    assert len(report.warnings) == 1


def test_msvc_diagnostics():
    report = parse_build_log(MSVC)
    assert len(report.errors) == 1
    assert report.errors[0].code == "C2065"
    assert report.errors[0].line == 13
    assert len(report.warnings) == 1


def test_link_errors():
    report = parse_build_log(LINK)
    assert report.errors == []
    assert len(report.link_errors) == 1
    assert "checksum" in report.link_errors[0]["symbol"]


def test_msvc_link_error_keeps_full_symbol():
    report = parse_build_log(MSVC_LINK)
    assert len(report.link_errors) == 1
    symbol = report.link_errors[0]["symbol"]
    assert "checksum" in symbol
    assert "sandbox::" in symbol
    assert symbol != "int"


def test_ctest_failure():
    report = parse_test_log(CTEST_FAIL)
    assert [f["name"] for f in report.failed] == ["ring_buffer"]
    assert report.totals == {"failed": 1, "total": 4, "passed": 3}
    assert report.passed == ["fd_owner"]
    assert report.assertions and "pop()" in report.assertions[0]["text"]


def test_ctest_timeout():
    report = parse_test_log(CTEST_TIMEOUT)
    assert report.failed[0]["status"] == "Timeout"


def test_ctest_segfault():
    report = parse_test_log(CTEST_SEGV)
    assert report.failed[0]["status"] == "SEGFAULT"
    assert report.crash_markers


def test_empty_log_is_not_a_crash():
    assert parse_build_log("").errors == []
    assert parse_test_log("").failed == []


@pytest.mark.parametrize("location,file", [
    (r"C:\repo\test\options.cpp(1558)", r"C:\repo\test\options.cpp"),
    ("/repo/test/options.cpp:1558", "/repo/test/options.cpp"),
])
def test_catch2_assertion_reaches_worker_summary_after_context_collapse(location, file):
    log = (f"{location}: FAILED:\r\n"
           '  REQUIRE( result.count("job") == 4 )\r\n'
           "with expansion:\r\n  1 == 4\r\n"
           "0% tests passed, 1 tests failed out of 1\r\n"
           "  1 - options (Failed)\r\n")
    report = parse_test_log(log)
    assert report.failure_kind == "assertion"
    evidence = report.as_dict()["assertions"][0]
    assert evidence["file"] == file and evidence["line"] == 1558
    assert evidence["log_line"] == 1
    assert 'result.count("job") == 4' in evidence["text"]
    assert "1 == 4" in evidence["text"]
    summary = _failed_tests_summary(report, ["ctest", "--output-on-failure"])
    message = tool_result_message("test-call", "run_test", json.dumps({"ok": False, "summary": summary}))
    collapsed = json.loads(_collapse(message)["content"])
    assert 'result.count("job") == 4' in collapsed["summary"]
    assert "1 == 4" in collapsed["summary"]


@pytest.mark.parametrize("body", [
    "test.cpp(12): FAILED:\n  due to unexpected exception with message:\n",
    "test.cpp:12: FAILED:\n",
    "FAILED:\n  REQUIRE( x == 4 )\n",
    "test.cpp:12: PASSED:\n  REQUIRE( x == 4 )\n",
])
def test_catch2_non_assertion_output_is_not_promoted(body):
    report = parse_test_log(body + "  1 - options (Failed)\n")
    assert report.assertions == []
    assert report.failure_kind == "failed"
