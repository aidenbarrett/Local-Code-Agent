"""Static standards ratchet: no file may gain a finding, and new files start clean.

The rules live in internal/docs/engineering-standards.md; the checker configuration lives in
pyproject.toml. This gate runs the pinned checkers, counts findings per (checker, file,
code) and compares them with internal/static-standards-baseline.json:

* any count above its baseline, or any key absent from the baseline, fails;
* any count below its baseline also fails until the baseline is lowered with ``--update``,
  so fixed debt cannot silently come back;
* ``--update`` only ever lowers or removes entries. Raising one is a reviewed, hand-edited
  change to the baseline, never a flag.

mypy runs once per target platform, because platform-guarded code (Windows job objects,
POSIX process groups) is unreachable, and so unchecked, on the other platform.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from local_agent.provenance import PRODUCT_PYTHON_FILES, PRODUCT_PYTHON_ROOTS

BASELINE_SCHEMA: Final = "lca.static-standards-baseline/1"
BASELINE_PATH: Final = PurePosixPath("internal/static-standards-baseline.json")
MYPY_PLATFORMS: Final = ("linux", "win32")
STATIC_ROOTS: Final = (
    "internal/local_agent",
    "internal/devtools",
    "internal/serving",
    "internal/scripts",
    "internal/terminal_ui.py",
)


def validate_root_inventory(static_roots: tuple[str, ...] = STATIC_ROOTS) -> None:
    """Refuse a gate that omits a live Python surface from source identity."""
    covered = tuple(PurePosixPath(root) for root in static_roots)
    required = (*PRODUCT_PYTHON_ROOTS, *PRODUCT_PYTHON_FILES)
    omitted = tuple(
        path for path in required
        if not any(PurePosixPath(path) == root or PurePosixPath(path).is_relative_to(root)
                   for root in covered)
    )
    if omitted:
        raise RuntimeError(
            "static root inventory omits live product Python surfaces: " + ", ".join(omitted)
        )

# (checker, posix path, code) -> count
FindingKey = tuple[str, str, str]
Counts = Mapping[FindingKey, int]


@dataclass(frozen=True, slots=True)
class Drift:
    """The difference between measured findings and the recorded baseline."""

    regressions: tuple[tuple[FindingKey, int, int], ...]
    improvements: tuple[tuple[FindingKey, int, int], ...]

    @property
    def clean(self) -> bool:
        return not self.regressions and not self.improvements


def compare(measured: Counts, baseline: Counts) -> Drift:
    """Classify every key whose measured count differs from the baseline."""
    regressions: list[tuple[FindingKey, int, int]] = []
    improvements: list[tuple[FindingKey, int, int]] = []
    for key in sorted(set(measured) | set(baseline)):
        now, allowed = measured.get(key, 0), baseline.get(key, 0)
        if now > allowed:
            regressions.append((key, allowed, now))
        elif now < allowed:
            improvements.append((key, allowed, now))
    return Drift(tuple(regressions), tuple(improvements))


def lowered(measured: Counts, baseline: Counts) -> dict[FindingKey, int]:
    """The baseline after accepting improvements only. Refuses to absorb any regression."""
    drift = compare(measured, baseline)
    if drift.regressions:
        raise ValueError("refusing to raise the baseline; fix the new findings instead")
    return {key: n for key, n in measured.items() if n > 0}


def _posix(path: str) -> str:
    # Checkers print native separators; the baseline is keyed by POSIX paths on every host.
    return path.replace("\\", "/")


def parse_mypy_json(lines: Iterable[str], platform: str) -> Counter[FindingKey]:
    counts: Counter[FindingKey] = Counter()
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or record.get("severity") != "error":
            continue
        file, code = record.get("file"), record.get("code")
        if not isinstance(file, str) or not isinstance(code, str):
            raise ValueError(f"unexpected mypy record: {line!r}")
        counts[(f"mypy-{platform}", _posix(file), code)] += 1
    return counts


def parse_ruff_json(text: str) -> Counter[FindingKey]:
    counts: Counter[FindingKey] = Counter()
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError("unexpected ruff output")
    root = Path.cwd()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("unexpected ruff record")
        file, code = record.get("filename"), record.get("code")
        if not isinstance(file, str) or not isinstance(code, str):
            raise ValueError(f"unexpected ruff record: {record!r}")
        path = Path(file)
        relative = path.relative_to(root) if path.is_absolute() else path
        counts[("ruff", _posix(str(relative)), code)] += 1
    return counts


def _run(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    # Fixed argv lists, never a shell: nothing here is interpolated into a command line.
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603


def measure(root: Path, mypy: list[str], ruff: list[str]) -> Counter[FindingKey]:
    validate_root_inventory()
    counts: Counter[FindingKey] = Counter()
    for platform in MYPY_PLATFORMS:
        done = _run([
            *mypy, "--platform", platform, "--output", "json", "--no-incremental", *STATIC_ROOTS,
        ], root)
        # mypy exits 1 when it reports findings and 2 when it could not run at all.
        if done.returncode not in (0, 1):
            raise RuntimeError(f"mypy ({platform}) failed to run:\n{done.stderr}{done.stdout}")
        counts.update(parse_mypy_json(done.stdout.splitlines(), platform))
    done = _run([
        *ruff, "check", "--no-cache", "--output-format", "json", "--exit-zero", *STATIC_ROOTS,
    ], root)
    if done.returncode != 0:
        raise RuntimeError(f"ruff failed to run:\n{done.stderr}")
    counts.update(parse_ruff_json(done.stdout))
    return counts


def load_baseline(path: Path) -> dict[FindingKey, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != BASELINE_SCHEMA:
        raise ValueError(f"{path} is not a {BASELINE_SCHEMA} document")
    findings = data.get("findings")
    if not isinstance(findings, dict):
        raise ValueError(f"{path} has no findings table")
    counts: dict[FindingKey, int] = {}
    for checker, files in findings.items():
        if not isinstance(files, dict):
            raise ValueError(f"{path}: {checker} must map files to codes")
        for file, codes in files.items():
            if not isinstance(codes, dict):
                raise ValueError(f"{path}: {checker} {file} must map codes to counts")
            for code, n in codes.items():
                if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                    raise ValueError(f"{path}: {checker} {file} {code} must be a positive count")
                counts[(checker, file, code)] = n
    return counts


def dump_baseline(counts: Counts) -> str:
    table: dict[str, dict[str, dict[str, int]]] = {}
    for (checker, file, code), n in sorted(counts.items()):
        table.setdefault(checker, {}).setdefault(file, {})[code] = n
    total = sum(counts.values())
    return json.dumps({"schema": BASELINE_SCHEMA, "total": total, "findings": table},
                      indent=1, sort_keys=True) + "\n"


def _describe(rows: Iterable[tuple[FindingKey, int, int]]) -> str:
    return "\n".join(f"  {checker} {file} [{code}]: baseline {old}, now {new}"
                     for (checker, file, code), old, new in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--update", action="store_true",
                        help="lower the baseline to the measured counts (never raises it)")
    parser.add_argument("--mypy", nargs="+", default=[sys.executable, "-m", "mypy"],
                        help="mypy command (default: the pinned mypy in this interpreter)")
    parser.add_argument("--ruff", nargs="+", default=[sys.executable, "-m", "ruff"],
                        help="ruff command (default: the pinned ruff in this interpreter)")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    baseline_file = root / BASELINE_PATH
    baseline = load_baseline(baseline_file)
    measured = measure(root, args.mypy, args.ruff)
    if args.update:
        new = lowered(measured, baseline)
        baseline_file.write_text(dump_baseline(new), encoding="utf-8")
        sys.stdout.write(f"baseline lowered: {sum(baseline.values())} -> {sum(new.values())}\n")
        return 0
    drift = compare(measured, baseline)
    if drift.regressions:
        sys.stderr.write("New static-standard findings (fix them; see "
                         "internal/docs/engineering-standards.md):\n")
        sys.stderr.write(_describe(drift.regressions) + "\n")
    if drift.improvements:
        sys.stderr.write("Findings were fixed; lock that in with "
                         "`python internal/devtools/check_static_standards.py --update`:\n")
        sys.stderr.write(_describe(drift.improvements) + "\n")
    if not drift.clean:
        return 1
    sys.stdout.write(f"static standards: {sum(measured.values())} recorded findings, none new\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
