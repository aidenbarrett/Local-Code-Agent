"""A failed test run names where its first assertion failed, after the expression.

The expression stays first so a long path can never hide the cause; the location is
repository-relative when the file is under the tested tree.
"""
from __future__ import annotations

from local_agent.tools.logs import parse_test_log
from local_agent.tools.testing_tools import _failed_tests_summary

COMMAND = ["ctest", "--test-dir", "build"]


def _log(path: str) -> str:
    return "\n".join((
        "1/2 Test #1: options ..........***Failed    0.01 sec",
        f"{path}:780: FAILED:",
        "  CHECK( positional[4] == 0xab )",
        "with expansion:",
        "  154 == 171",
        "50% tests passed, 1 tests failed out of 2",
        "The following tests FAILED:",
        "\t  1 - options (Failed)",
    ))


def test_the_location_is_repository_relative_and_follows_the_expression(tmp_path):
    source = tmp_path / "test" / "options.cpp"
    summary = _failed_tests_summary(parse_test_log(_log(str(source))), COMMAND, tmp_path)
    assert ("first assertion evidence: CHECK( positional[4] == 0xab ) with expansion: "
            "154 == 171 at test/options.cpp:780 -- command:") in summary
    assert str(tmp_path) not in summary


def test_a_file_outside_the_tree_keeps_its_path_bounded(tmp_path):
    outside = "/elsewhere/" + "deep/" * 60 + "options.cpp"
    summary = _failed_tests_summary(parse_test_log(_log(outside)), COMMAND, tmp_path)
    location = summary.split(" at ", 1)[1].split(" -- command:")[0]
    assert location.startswith("…") and location.endswith("options.cpp:780")
    assert len(location) <= 170


def test_a_marker_without_a_location_is_unchanged(tmp_path):
    log = "Segmentation fault\nThe following tests FAILED:\n\t  1 - crash (SEGFAULT)\n"
    summary = _failed_tests_summary(parse_test_log(log), COMMAND, tmp_path)
    assert " at " not in summary.split(" -- command:")[0].split("evidence:")[-1]
