#!/usr/bin/env python3
"""Analyse generation-2 pilot rows under the pre-registered endpoint policy.

Usage:
    python measurement/analyze_endpoints.py run-control.json run-narrow.json run-skill.json

The input files may be separate condition files. Rows are concatenated; the
endpoint module refuses to make a task-level decision unless exactly three
valid draws exist for that task/condition.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from evaluation.endpoints import analyse  # noqa: E402


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected an object containing a rows array")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the frozen four-endpoint and 2-of-3 repeat policy."
    )
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, help="also write the analysis as JSON")
    args = parser.parse_args(argv)

    rows: list[dict] = []
    for path in args.results:
        rows.extend(_rows(path))

    result = analyse(rows)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
