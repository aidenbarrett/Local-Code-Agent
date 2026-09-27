from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "devtools" / "annotate_test_failures.py"
SPEC = importlib.util.spec_from_file_location("lca_annotate_test_failures", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
annotate = importlib.util.module_from_spec(SPEC)
sys.modules["lca_annotate_test_failures"] = annotate
SPEC.loader.exec_module(annotate)

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
<testcase classname="internal.tests.unit.test_a" name="test_passes" time="0.1"/>
<testcase classname="internal.tests.unit.test_a" name="test_fails[x,y]" time="0.2">
<failure message="AssertionError: assert 1 == 2">def test_fails():
&gt;       assert 1 == 2
E       assert 1 == 2
100% sure</failure></testcase>
<testcase classname="internal.tests.unit.test_b" name="test_errors" time="0.0">
<error message="fixture 'sandbox' failed">setup blew up</error></testcase>
<testcase classname="internal.tests.unit.test_b" name="test_skipped"><skipped message="no"/></testcase>
</testsuite></testsuites>
"""


def test_each_failed_or_errored_test_becomes_one_error_annotation(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(REPORT, encoding="utf-8")
    lines = annotate.annotation_lines(report)
    assert len(lines) == 2
    first, second = lines
    # The title names the test; property escaping keeps ':' and ',' from ending it.
    assert first.startswith("::error title=failure%3A internal.tests.unit.test_a%3A%3Atest_fails[x%2Cy]::")
    assert "AssertionError: assert 1 == 2%0A" in first and "100%25 sure" in first
    assert "\n" not in first
    assert second.startswith("::error title=error%3A internal.tests.unit.test_b%3A%3Atest_errors::")


def test_a_missing_report_is_itself_an_annotation_not_a_crash(tmp_path, capsys):
    assert annotate.main([str(tmp_path / "absent.xml")]) == 0
    assert capsys.readouterr().out.startswith("::error title=no JUnit report::")


def test_annotations_are_capped(tmp_path):
    cases = "".join(f'<testcase classname="c" name="t{i}"><failure message="m"/></testcase>'
                    for i in range(annotate.MAX_ANNOTATIONS + 3))
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    lines = annotate.annotation_lines(report)
    assert len(lines) == annotate.MAX_ANNOTATIONS + 1
    assert lines[-1] == "::error title=more failures::3 more not annotated"
