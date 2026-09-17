"""Deterministic log parsing.

The highest-value thing in the whole system. A 48 MB build log is not an LLM
problem, it is a regex problem. The model only gets involved once the log has
been reduced to a handful of structured diagnostics it can actually reason
about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# gcc / clang:  src/queue.cpp:81:17: error: use of undeclared identifier 'count'
_GNU = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s+"
    r"(?P<severity>error|warning|note|fatal error):\s+(?P<message>.*)$"
)

# msvc:  C:\src\queue.cpp(81,17): error C2065: 'count': undeclared identifier
_MSVC = re.compile(
    r"^(?P<file>[A-Za-z]:\\[^(]+|[^\s(][^(]*)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*"
    r"(?P<severity>error|warning|fatal error)\s+(?P<code>[A-Z]+\d+)\s*:\s*(?P<message>.*)$"
)

_LINK_PATTERNS = (
    re.compile(r"undefined reference to [`'\"](?P<symbol>[^`'\"]+)"),
    re.compile(r"undefined symbol:\s*(?P<symbol>\S+)"),
    # MSVC prints a C++ signature, often including spaces, return type and calling
    # convention, followed by "referenced in function ...". Capturing only \S+
    # reduced symbols such as `int sandbox::checksum(...)` to the useless `int`.
    re.compile(
        r"error LNK2019: unresolved external symbol (?P<symbol>.+?)"
        r"(?: referenced in function|$)"
    ),
    re.compile(r"ld: symbol\(s\) not found for (?P<symbol>\S+)"),
)

_CRASH_PATTERNS = (
    re.compile(r"Segmentation fault"),
    re.compile(r"SIGSEGV|SIGABRT|SIGFPE|SIGBUS"),
    re.compile(r"Assertion .* failed\."),
    re.compile(r"terminate called after throwing an instance of"),
    re.compile(r"AddressSanitizer:|UndefinedBehaviorSanitizer:|ThreadSanitizer:"),
    re.compile(r"std::bad_alloc|std::out_of_range|std::runtime_error"),
)

# ctest:  "  3 - ring_pop_order (Failed)"  /  "(Timeout)" / "(SEGFAULT)"
_CTEST_FAIL = re.compile(
    r"^\s*(?P<index>\d+)\s*-\s*(?P<name>\S+)\s*\((?P<status>Failed|Timeout|SEGFAULT|"
    r"Subprocess aborted|Exception|Not Run|Child aborted)\)"
)
_CTEST_SUMMARY = re.compile(
    r"^(?P<passed>\d+)% tests passed,\s*(?P<failed>\d+) tests failed out of "
    r"(?P<total>\d+)"
)
_CTEST_PASS_LINE = re.compile(r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(?P<name>\S+)\s+\.+\s+Passed")


@dataclass
class Diagnostic:
    file: str
    line: int
    column: int | None
    severity: str
    message: str
    log_line: int
    code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "message": self.message,
            "log_line": self.log_line,
        }
        if self.column is not None:
            out["column"] = self.column
        if self.code:
            out["code"] = self.code
        return out


@dataclass
class BuildLogReport:
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    link_errors: list[dict[str, Any]] = field(default_factory=list)
    cmake_errors: list[dict[str, Any]] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)

    def as_dict(self, max_items: int = 10) -> dict[str, Any]:
        return {
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [d.as_dict() for d in self.errors[:max_items]],
            "warnings": [d.as_dict() for d in self.warnings[:max_items]],
            "link_errors": self.link_errors[:max_items],
            "cmake_errors": self.cmake_errors[:max_items],
            "tail": self.tail,
        }


@dataclass
class TestLogReport:
    failed: list[dict[str, Any]] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    crash_markers: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)

    def as_dict(self, max_items: int = 10) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "failed": self.failed[:max_items],
            "passed_count": len(self.passed),
            "crash_markers": self.crash_markers[:max_items],
            "assertions": self.assertions[:max_items],
            "tail": self.tail,
        }


def _tail(lines: list[str], n: int = 25) -> list[str]:
    return [ln.rstrip() for ln in lines[-n:] if ln.strip()]


def _dedupe(diags: Iterable[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[str, int, str]] = set()
    out: list[Diagnostic] = []
    for d in diags:
        key = (d.file, d.line, d.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


# ESC [ ... final-byte, plus the OSC form terminated by BEL or ST. Compilers
# and ctest colour their output when they think a terminal is attached, and
# CLICOLOR_FORCE or a CI wrapper can make that happen where nobody expects it.
# Every parser below is a regex over exact strings, so one invisible escape in
# front of "0% tests passed" turns a real failure into "totals: {}", which the
# tool layer then reports as "not configured or not built". Strip once, here,
# before anything reads a character.
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_build_log(text: str) -> BuildLogReport:
    text = strip_ansi(text)
    lines = text.splitlines()
    report = BuildLogReport()
    raw_errors: list[Diagnostic] = []
    raw_warnings: list[Diagnostic] = []

    for idx, line in enumerate(lines, start=1):
        m = _GNU.match(line.strip()) or _MSVC.match(line.strip())
        if m:
            gd = m.groupdict()
            sev = gd["severity"].replace("fatal error", "error")
            diag = Diagnostic(
                file=gd["file"].strip(),
                line=int(gd["line"]),
                column=int(gd["col"]) if gd.get("col") else None,
                severity=sev,
                message=gd["message"].strip(),
                log_line=idx,
                code=gd.get("code"),
            )
            if sev == "error":
                raw_errors.append(diag)
            elif sev == "warning":
                raw_warnings.append(diag)
            continue

        for pat in _LINK_PATTERNS:
            lm = pat.search(line)
            if lm:
                report.link_errors.append(
                    {
                        "symbol": lm.group("symbol"),
                        "log_line": idx,
                        "text": line.strip()[:300],
                    }
                )
                break

        if line.startswith("CMake Error") or line.startswith("CMake Deprecation"):
            report.cmake_errors.append({"log_line": idx, "text": line.strip()[:300]})

    report.errors = _dedupe(raw_errors)
    report.warnings = _dedupe(raw_warnings)
    report.tail = _tail(lines)
    return report


def parse_test_log(text: str) -> TestLogReport:
    text = strip_ansi(text)
    lines = text.splitlines()
    report = TestLogReport()

    for idx, line in enumerate(lines, start=1):
        fm = _CTEST_FAIL.match(line)
        if fm:
            report.failed.append(
                {
                    "name": fm.group("name"),
                    "status": fm.group("status"),
                    "log_line": idx,
                }
            )
            continue

        pm = _CTEST_PASS_LINE.match(line)
        if pm:
            report.passed.append(pm.group("name"))
            continue

        sm = _CTEST_SUMMARY.match(line.strip())
        if sm:
            report.totals = {
                "failed": int(sm.group("failed")),
                "total": int(sm.group("total")),
                "passed": int(sm.group("total")) - int(sm.group("failed")),
            }
            continue

        for pat in _CRASH_PATTERNS:
            if pat.search(line):
                entry = {"log_line": idx, "text": line.strip()[:300]}
                if "Assertion" in line:
                    report.assertions.append(entry)
                else:
                    report.crash_markers.append(entry)
                break

    # Deduplicate ctest's habit of listing a failure twice.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for f in report.failed:
        if f["name"] in seen:
            continue
        seen.add(f["name"])
        unique.append(f)
    report.failed = unique

    report.tail = _tail(lines)
    return report


def read_chunk(path: Path, start_line: int, end_line: int) -> dict[str, Any]:
    start_line = max(1, start_line)
    if end_line < start_line:
        end_line = start_line
    out: list[str] = []
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, line in enumerate(fh, start=1):
            total = idx
            if start_line <= idx <= end_line:
                out.append(f"{idx}: {line.rstrip()}")
            elif idx > end_line:
                total = None  # unknown; do not read the whole file to count
                break
    return {
        "start_line": start_line,
        "end_line": start_line + len(out) - 1 if out else start_line,
        "lines": out,
        "eof_reached": total is not None,
    }
