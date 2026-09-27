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

rewrites ``internal/tests/shard-weights.json`` from JUnit reports.
"""
from __future__ import annotations

import json
import statistics
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

WEIGHTS = Path(__file__).resolve().parents[1] / "tests" / "shard-weights.json"


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
    default = statistics.median(known) if known else 1.0
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in sorted(set(files), key=lambda f: (-weights.get(f, default), f)):
        target = min(range(count), key=lambda k: (loads[k], k))
        shards[target].append(name)
        loads[target] += weights.get(name, default)
    return shards


def load_weights(path: Path = WEIGHTS) -> dict[str, float]:
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


def weights_from_junit(reports: Sequence[Path]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for report in reports:
        # Our own pytest's report, not untrusted input.
        for case in ET.parse(report).getroot().iter("testcase"):  # noqa: S314
            name = file_of(case.get("classname", ""))
            totals[name] = totals.get(name, 0.0) + float(case.get("time") or 0.0)
    return {k: round(v, 2) for k, v in sorted(totals.items())}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or args[0] != "update":
        sys.stderr.write("usage: ci_shards.py update <junit.xml> [...]\n")
        return 2
    weights = weights_from_junit([Path(a) for a in args[1:]])
    WEIGHTS.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"{len(weights)} files, {sum(weights.values()):.0f}s recorded\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
