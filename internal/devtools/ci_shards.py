#!/usr/bin/env python3
"""Split the pytest suite into duration-balanced shards, whole test files at a time.

Every test still runs: the shards are a partition of what pytest collected, so the
union of all shards is the authoritative suite. Only wall-clock time changes, because
the shards run on separate CI runners at once.

Whole files, not single tests, go to a shard, so module fixtures (built C++ trees,
sandboxes) are set up once per file exactly as in a serial run. Files are placed
longest first onto the least-loaded shard (LPT scheduling), using per-file seconds
recorded from a real run. A file with no recorded time is given the median.

    python internal/devtools/ci_shards.py update pytest-junit.xml [more.xml ...]

rewrites ``internal/tests/shard-weights.json`` from JUnit reports, and

    python internal/devtools/ci_shards.py report pytest-junit.xml

prints one ``::notice`` with the run's test time and its slowest files, so a CI
run's time profile is readable from the checks API on every platform.
"""
from __future__ import annotations

import json
import statistics
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

MIN_WEIGHT = 0.05
TESTS = Path(__file__).resolve().parents[1] / "tests"
WEIGHTS = TESTS / "shard-weights.json"
# Windows builds the C++ fixtures about eight times slower than Linux, and not evenly
# across files, so it balances on its own recorded times when they exist.
PLATFORM_WEIGHTS = {"win32": TESTS / "shard-weights-win32.json"}
# Heavy files whose tests share no module, class or session fixture: each test is its
# own unit, so one long file cannot hold a whole shard. Every other file moves whole,
# exactly as it runs serially.
SPLIT_FILES = frozenset({
    "internal/tests/integration/test_condition_purity.py",
    "internal/tests/integration/test_oracle_isolation.py",
    "internal/tests/integration/test_endpoint_row_contract.py",
    "internal/tests/integration/test_build_and_diagnose.py",
    "internal/tests/unit/test_candidate_change_journey.py",
    "internal/tests/unit/test_acceptance_journeys.py",
    "internal/tests/unit/test_session_textual_retained_results.py",
    "internal/tests/unit/test_audit_fixes.py",
})


def unit_of(nodeid: str) -> str:
    """The shard unit a collected test belongs to: its file, or itself in a split file."""
    path = nodeid.split("::", 1)[0]
    return nodeid if path in SPLIT_FILES else path


def parse_shard(spec: str) -> tuple[int, int]:
    """'k/n' with 1 <= k <= n, as (k, n)."""
    try:
        index_text, count_text = spec.split("/")
        index, count = int(index_text), int(count_text)
    except ValueError as exc:
        raise ValueError(f"shard must look like k/n, got {spec!r}") from exc
    if not 1 <= index <= count:
        raise ValueError(f"shard index must be in 1..{count}, got {index}")
    return index, count


def plan(files: Sequence[str], weights: Mapping[str, float], count: int) -> list[list[str]]:
    """Partition ``files`` into ``count`` shards of roughly equal recorded time."""
    if count < 1:
        raise ValueError("shard count must be positive")
    known = [weights[f] for f in files if f in weights]
    default = max(statistics.median(known), MIN_WEIGHT) if known else 1.0

    def weight(name: str) -> float:
        # A file recorded at 0 s still costs collection and setup; never weightless,
        # or every such file would pile onto the first shard.
        return max(weights.get(name, default), MIN_WEIGHT)

    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in sorted(set(files), key=lambda f: (-weight(f), f)):
        target = min(range(count), key=lambda k: (loads[k], len(shards[k]), k))
        shards[target].append(name)
        loads[target] += weight(name)
    return shards


def weights_path(platform: str = sys.platform) -> Path:
    candidate = PLATFORM_WEIGHTS.get(platform)
    return candidate if candidate is not None and candidate.is_file() else WEIGHTS


def load_weights(path: Path | None = None) -> dict[str, float]:
    path = weights_path() if path is None else path
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items()}


def file_of(classname: str) -> str:
    """JUnit classname ('internal.tests.unit.test_x.TestY') to 'internal/tests/unit/test_x.py'."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        if parts[end - 1].startswith("test_") or parts[end - 1] == "conftest":
            return "/".join(parts[:end]) + ".py"
    return "/".join(parts) + ".py"


def nodeid_of(classname: str, name: str) -> str:
    """JUnit classname and name back to the pytest node id."""
    path = file_of(classname)
    module = path[:-3].replace("/", ".")
    inner = classname[len(module):].lstrip(".")
    return "::".join([path, *inner.split(".")] if inner else [path]) + f"::{name}"


def weights_from_junit(reports: Sequence[Path], *, by_unit: bool = False) -> dict[str, float]:
    """Seconds per file, or per shard unit (split files by test) when ``by_unit``."""
    totals: dict[str, float] = {}
    for report in reports:
        # Our own pytest's report, not untrusted input.
        for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314
            classname = case.get("classname", "")
            key = file_of(classname)
            if by_unit:
                key = unit_of(nodeid_of(classname, case.get("name", "")))
            totals[key] = totals.get(key, 0.0) + float(case.get("time") or 0.0)
    return {k: round(v, 2) for k, v in sorted(totals.items())}


REPORT_FILES = 15


def report_line(weights: Mapping[str, float]) -> str:
    """One ::notice with this run's total and its slowest files, for the checks API."""
    slowest = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:REPORT_FILES]
    body = "%0A".join(f"{seconds:7.1f}s  {name}" for name, seconds in slowest)
    return f"::notice title=test time {sum(weights.values()):.0f}s (slowest files)::{body}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) >= 2 and args[0] == "report":
        reports = [Path(a) for a in args[1:] if Path(a).is_file()]
        if reports:
            sys.stdout.write(report_line(weights_from_junit(reports)) + "\n")
        return 0
    if len(args) < 2 or args[0] != "update":
        sys.stderr.write("usage: ci_shards.py update|report <junit.xml> [...]\n")
        return 2
    weights = weights_from_junit([Path(a) for a in args[1:]], by_unit=True)
    WEIGHTS.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"{len(weights)} shard units, {sum(weights.values()):.0f}s recorded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
