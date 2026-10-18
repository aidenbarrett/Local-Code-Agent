"""Typed facts about why a configured check failed (lca.task-result/3 ``check_failure``).

A configured build or test check is run by the controller, and its build and test tools
already return structured reports (``parse_build_log`` / ``parse_test_log``). This module
keeps a bounded, repository-relative projection of the failing step's report with the
task result, so a later "why did that fail?" can be answered from typed facts in a fixed
layout. Nothing here reads prose: when the typed report is absent or malformed, the
projection is None and the diagnosis stays generic.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_STEPS = {"build_target": "build", "run_test": "tests"}
_KINDS = frozenset({
    "compile", "link", "configure", "assertion", "crash", "timeout", "failed",
})
MAX_TESTS = 10
MAX_NAME_CHARS = 200
MAX_TEXT_CHARS = 300
MAX_LOCATION_CHARS = 160
MAX_ARGV = 32
MAX_ARG_CHARS = 200
_KEYS = frozenset({"step", "kind", "tests", "test_count", "first", "command"})
_FIRST_KEYS = frozenset({"location", "text"})


@dataclass(frozen=True)
class FirstFailure:
    location: str | None
    text: str


@dataclass(frozen=True)
class CheckFailure:
    """The failing step of a configured check, bounded and repository-relative."""

    step: str
    kind: str | None
    tests: tuple[str, ...]
    test_count: int
    first: FirstFailure | None
    command: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "tests": list(self.tests),
            "test_count": self.test_count,
            "first": (None if self.first is None
                      else {"location": self.first.location, "text": self.first.text}),
            "command": list(self.command),
        }


def _bounded(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _location(file: object, line: object, root: Path) -> str | None:
    """``path:line`` relative to the repository root, or None.

    A relative path is taken as relative to the root. Only a path whose canonical form
    lies inside the root is shown; anything outside it, or that cannot be resolved,
    keeps its message but loses the location.
    """
    if (not isinstance(file, str) or not file or not file.isprintable()
            or not isinstance(line, int) or isinstance(line, bool) or line < 1):
        return None
    path = Path(file)
    try:
        canonical_root = root.resolve()
        shown = (path if path.is_absolute() else root / path).resolve().relative_to(
            canonical_root).as_posix()
    except (OSError, ValueError):
        return None
    if shown in {"", "."}:
        return None
    if len(shown) > MAX_LOCATION_CHARS:
        shown = "…" + shown[-(MAX_LOCATION_CHARS - 1):]
    return f"{shown}:{line}"


def _valid_location(location: object) -> bool:
    """A retained location is a bounded repository-relative ``path:line``."""
    if not isinstance(location, str) or not location.isprintable():
        return False
    path, sep, line = location.rpartition(":")
    if not sep or not line.isdigit() or not path or len(path) > MAX_LOCATION_CHARS:
        return False
    parts = path.lstrip("…").split("/")
    return (not path.startswith(("/", "\\")) and ":" not in path and "\\" not in path
            and ".." not in parts)


def _first(step: str, data: Mapping[str, Any], root: Path) -> FirstFailure | None:
    """The first piece of typed evidence the step's report holds, or None."""
    if step == "tests":
        groups: Sequence[object] = (data.get("assertions"), data.get("crash_markers"))
    else:
        groups = (data.get("errors"), data.get("link_errors"), data.get("cmake_errors"))
    for group in groups:
        if not isinstance(group, list) or not group or not isinstance(group[0], Mapping):
            continue
        entry = group[0]
        text = entry.get("message") if step == "build" else entry.get("text")
        if not isinstance(text, str):
            text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        return FirstFailure(_location(entry.get("file"), entry.get("line"), root),
                            _bounded(text, MAX_TEXT_CHARS))
    return None


def check_failure_facts(history: Sequence[Any], root: Path) -> dict[str, Any] | None:
    """Project the last failing build/test step of a configured check, or None.

    ``history`` is the worker's tool-call records; only a step that executed and whose
    typed domain result is a failure is used, and only its structured ``evidence``.
    """
    failing = next((record for record in reversed(history)
                    if getattr(record, "name", None) in _STEPS
                    and getattr(record, "execution", None) == "ok"
                    and getattr(record, "domain", None) == "fail"), None)
    if failing is None:
        return None
    data = failing.evidence
    if not isinstance(data, Mapping):
        return None
    step = _STEPS[failing.name]
    kind = data.get("failure_kind")
    failed = data.get("failed") if step == "tests" else None
    names = tuple(
        _bounded(str(entry["name"]), MAX_NAME_CHARS)
        for entry in (failed if isinstance(failed, list) else [])
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    )
    command = data.get("command")
    argv = tuple(_bounded(str(arg), MAX_ARG_CHARS)
                 for arg in (command if isinstance(command, list) else [])[:MAX_ARGV])
    facts = CheckFailure(
        step=step,
        kind=kind if kind in _KINDS else None,
        tests=names[:MAX_TESTS],
        test_count=_failed_count(step, data, len(names)),
        first=_first(step, data, root),
        command=argv,
    )
    return facts.as_payload() if _substantive(facts) else None


def _failed_count(step: str, data: Mapping[str, Any], named: int) -> int:
    """How many tests failed: the report's own total, which outlives the bounded
    list of names, when it is a valid count at least as large as that list."""
    if step != "tests":
        return 0
    totals = data.get("totals")
    total = totals.get("failed") if isinstance(totals, Mapping) else None
    if isinstance(total, int) and not isinstance(total, bool) and total >= named:
        return total
    return named


def _substantive(facts: CheckFailure) -> bool:
    """Facts that say something about the failure; otherwise the diagnosis stays generic.

    A test failure needs a positive failing count; a build failure needs its kind or
    its first error. An empty report must never render as "0 test(s)" or a bare
    "the build failed" with false precision.
    """
    if facts.step == "tests":
        return facts.test_count > 0
    return facts.kind is not None or facts.first is not None


def project_check_failure(metrics: Mapping[str, Any]) -> dict[str, Any] | None:
    """The retained ``check_failure`` block for a result's controller-set metrics."""
    facts = read_check_failure(metrics.get("check_failure"))
    return None if facts is None else facts.as_payload()


def _str(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _valid_lists(value: Mapping[str, Any]) -> bool:
    tests, count, command = value["tests"], value["test_count"], value["command"]
    return (
        isinstance(tests, list) and len(tests) <= MAX_TESTS
        and all(_str(name, MAX_NAME_CHARS) is not None for name in tests)
        and isinstance(count, int) and not isinstance(count, bool) and count >= len(tests)
        and isinstance(command, list) and len(command) <= MAX_ARGV
        and all(isinstance(arg, str) and len(arg) <= MAX_ARG_CHARS for arg in command)
    )


def _read_first(first: object) -> FirstFailure | None | bool:
    """The first failure, None when absent, or False when malformed."""
    if first is None:
        return None
    if not isinstance(first, Mapping) or set(first) != _FIRST_KEYS:
        return False
    text, location = _str(first["text"], MAX_TEXT_CHARS), first["location"]
    location_ok = location is None or _valid_location(location)
    return FirstFailure(location, text) if text is not None and location_ok else False


def read_check_failure(value: object) -> CheckFailure | None:
    """A retained ``check_failure`` value as typed facts; None when absent or malformed.

    Malformed facts are never an integrity failure of the task result: the diagnosis
    simply falls back to its generic form rather than inventing precision.
    """
    if not isinstance(value, Mapping) or set(value) != _KEYS:
        return None
    step, kind = value["step"], value["kind"]
    shape_ok = (step in set(_STEPS.values()) and (kind is None or kind in _KINDS)
                and _valid_lists(value))
    first = _read_first(value["first"])
    if not shape_ok or first is False:
        return None
    facts = CheckFailure(step, kind, tuple(value["tests"]), value["test_count"],
                         first if isinstance(first, FirstFailure) else None,
                         tuple(value["command"]))
    return facts if _substantive(facts) else None


def render_check_failure(facts: CheckFailure) -> list[str]:
    """The fixed-layout lines describing a recorded check failure."""
    # "failed" is ctest's plain status; naming it again adds nothing.
    kind = f" ({facts.kind})" if facts.kind and facts.kind != "failed" else ""
    if facts.step == "tests":
        lines = [f"Tests failed{kind}: {facts.test_count} test(s)."]
        if facts.tests:
            more = facts.test_count - len(facts.tests)
            lines.append("Failing tests: " + ", ".join(facts.tests)
                         + (f" and {more} more" if more > 0 else ""))
    else:
        lines = [f"The build failed{kind}."]
    if facts.first is not None:
        label = "First failure" if facts.step == "tests" else "First error"
        where = f" {facts.first.location}" if facts.first.location else ""
        lines.append(f"{label}:{where}")
        lines.append(f"  {facts.first.text}")
    if facts.command:
        lines.append("Command: " + json.dumps(list(facts.command)))
    return lines


__all__ = [
    "CheckFailure", "FirstFailure", "check_failure_facts", "project_check_failure",
    "read_check_failure",
    "render_check_failure",
]
