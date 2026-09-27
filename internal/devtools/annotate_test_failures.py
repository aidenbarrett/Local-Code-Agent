#!/usr/bin/env python3
"""Turn failed tests in a pytest JUnit report into GitHub check-run annotations.

A red `pytest` job says only "Process completed with exit code 1" in its annotations,
and the job log is not always readable by whoever has to fix it. Emitting one
``::error`` workflow command per failed test puts the test id and the start of its
failure message where the checks API returns it.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_ANNOTATIONS = 50
MESSAGE_LINES = 6
MESSAGE_CHARS = 900


def _escape(text: str) -> str:
    """Workflow-command escaping: the data part and the property part differ."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def failures(report: Path) -> list[tuple[str, str]]:
    """(test id, message) for every failed or errored test case, in report order."""
    # Our own pytest's report from this job, not untrusted input.
    root = ET.parse(report).getroot()  # noqa: S314
    found: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        for outcome in ("failure", "error"):
            node = case.find(outcome)
            if node is None:
                continue
            test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (node.get("message") or "") + "\n" + (node.text or "")
            lines = [line for line in detail.splitlines() if line.strip()][:MESSAGE_LINES]
            found.append((f"{outcome}: {test_id}", "\n".join(lines)[:MESSAGE_CHARS]))
            break
    return found


def annotation_lines(report: Path) -> list[str]:
    found = failures(report)
    out = [f"::error title={_escape_property(title)}::{_escape(message) or 'failed'}"
           for title, message in found[:MAX_ANNOTATIONS]]
    if len(found) > MAX_ANNOTATIONS:
        extra = len(found) - MAX_ANNOTATIONS
        out.append(f"::error title=more failures::{extra} more not annotated")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: annotate_test_failures.py <junit.xml>\n")
        return 2
    report = Path(args[0])
    if not report.is_file():
        sys.stdout.write(f"::error title=no JUnit report::{_escape(str(report))} was not written\n")
        return 0
    for line in annotation_lines(report):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
