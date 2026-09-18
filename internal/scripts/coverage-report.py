#!/usr/bin/env python3
"""Turn coverage.xml into an inspectable baseline report.

Reporting only. This script never invents a fail-under threshold. Once a baseline
is deliberately recorded it can fail on an overall drop larger than the recorded
tolerance; with no baseline present it reports that fact and exits successfully.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


TOLERANCE = 0.5


def _parse(path: Path) -> tuple[float, dict[str, dict[str, float]]]:
    root = ET.parse(path).getroot()
    modules: dict[str, dict[str, float]] = {}
    total_statements = total_covered = 0
    for cls in root.iter("class"):
        filename = cls.get("filename") or "?"
        lines = list(cls.iter("line"))
        statements = len(lines)
        covered = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
        if not statements:
            continue
        entry = modules.setdefault(filename, {"statements": 0, "covered": 0})
        entry["statements"] += statements
        entry["covered"] += covered
        total_statements += statements
        total_covered += covered
    overall = 100.0 * total_covered / total_statements if total_statements else 0.0
    for entry in modules.values():
        entry["percent"] = round(100.0 * entry["covered"] / entry["statements"], 1)
    return round(overall, 2), modules


def _print_table(overall: float, modules: dict[str, dict[str, float]], limit: int) -> None:
    print(f"overall statement+branch coverage: {overall:.2f}%")
    print(f"modules measured: {len(modules)}")
    print()
    ranked = sorted(modules.items(), key=lambda kv: (kv[1]["percent"], -kv[1]["statements"]))
    print(f"{'least covered':<62s} {'cov':>6s} {'stmts':>7s}")
    for name, entry in ranked[:limit]:
        print(f"  {name:<60.60s} {entry['percent']:>5.1f}% {entry['statements']:>7d}")
    print()
    never = [name for name, entry in modules.items() if entry["covered"] == 0]
    if never:
        print(f"modules with zero covered statements ({len(never)}):")
        for name in sorted(never):
            print(f"  {name}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument(
        "--write-baseline", type=Path, nargs="?", const=Path("internal/coverage-baseline.json")
    )
    parser.add_argument("--against", type=Path)
    args = parser.parse_args(argv)

    if not args.xml.is_file():
        print(f"no coverage report at {args.xml}", file=sys.stderr)
        return 2

    overall, modules = _parse(args.xml)
    _print_table(overall, modules, args.limit)

    if args.write_baseline:
        payload = {
            "overall": overall,
            "tolerance_points": TOLERANCE,
            "modules": {name: entry["percent"] for name, entry in sorted(modules.items())},
        }
        args.write_baseline.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.write_baseline}")
        return 0

    if args.against:
        if not args.against.is_file():
            print(
                f"no baseline at {args.against} yet; nothing to compare. "
                "Run with --write-baseline once the number is understood."
            )
            return 0
        baseline = json.loads(args.against.read_text(encoding="utf-8"))
        tolerance = float(baseline.get("tolerance_points", TOLERANCE))
        drop = float(baseline["overall"]) - overall
        print(
            f"baseline {baseline['overall']:.2f}% -> now {overall:.2f}% "
            f"({'-' if drop > 0 else '+'}{abs(drop):.2f} points, tolerance {tolerance})"
        )
        regressed = [
            (name, baseline["modules"][name], modules[name]["percent"])
            for name in sorted(set(baseline.get("modules", {})) & set(modules))
            if baseline["modules"][name] - modules[name]["percent"] > tolerance
        ]
        if regressed:
            print("\nper-module regressions beyond tolerance:")
            for name, was, now in regressed:
                print(f"  {name}: {was:.1f}% -> {now:.1f}%")
        if drop > tolerance:
            print(f"\nCOVERAGE REGRESSION: overall dropped {drop:.2f} points", file=sys.stderr)
            return 1
        if regressed:
            print("\nOverall held, but named modules regressed. Not failing on that alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
