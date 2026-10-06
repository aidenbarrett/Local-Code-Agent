from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_SHA = re.compile(r"Reconciled against GitHub `main` at `([0-9a-fA-F]{40})`")
_QUALIFICATION = re.compile(
    r"^(?:deterministic-ci|real-model-ci|none|unknown|"
    r"hardware:[^/|\s]+/[^|\s]+ \(\d{4}-\d{2}-\d{2}\))$"
)
_JOURNEY = re.compile(r"\b(?:J|R)\d{2}[a-z]?(?:-[a-z0-9-]+)?\b")
_TEST = re.compile(r"(?P<path>internal/tests/[^\s|`]+\.py)::(?P<name>test_[A-Za-z0-9_]+)")
_PR = re.compile(r"^PR#\d+$")
_RUN = re.compile(r"^run:[^\s|]+$")


def recorded_sha(text: str) -> str:
    match = _SHA.search(text)
    if match is None:
        raise ValueError("CURRENT_STATE.md must contain one full reconciliation SHA")
    return match.group(1).lower()


def _load_acceptance(root: Path) -> ModuleType:
    path = root / "internal/scripts/acceptance-journeys.py"
    spec = importlib.util.spec_from_file_location("lca_acceptance_journeys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load acceptance journeys from {path}")
    module = importlib.util.module_from_spec(spec)
    internal = str(root / "internal")
    sys.path.insert(0, internal)
    # Restore, never just delete: a test session may already hold this module under
    # the same name, and dropping its entry would make the next import re-execute it.
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules[spec.name]
        else:
            sys.modules[spec.name] = previous
        sys.path.remove(internal)
    return module


def _journey_ids(root: Path) -> set[str]:
    module = _load_acceptance(root)
    return {row[0] for row in (*module.JOURNEYS, *module.REPO_JOURNEYS)}


def _ledger_rows(text: str) -> list[dict[str, str]]:
    marker = "## Capability ledger"
    if marker not in text:
        raise ValueError("CURRENT_STATE.md must contain a capability ledger")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    if len(rows) < 3:
        raise ValueError("capability ledger must contain a header and at least one row")
    header = [cell.strip() for cell in rows[0].strip("|").split("|")]
    expected = [
        "ID", "Public request", "Implemented behaviour", "Evidence",
        "Qualification", "Known limit", "Next action", "Owner",
    ]
    if header != expected:
        raise ValueError(f"capability ledger columns must be: {' | '.join(expected)}")
    parsed: list[dict[str, str]] = []
    for line in rows[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(expected):
            raise ValueError(f"capability ledger row has {len(cells)} columns, expected 8: {line}")
        parsed.append(dict(zip(expected, cells, strict=True)))
    return parsed


def validate_ledger(root: Path, text: str) -> None:
    rows = _ledger_rows(text)
    ids = [row["ID"] for row in rows]
    duplicates = sorted({capability_id for capability_id in ids if ids.count(capability_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate capability ledger ID(s): {', '.join(duplicates)}")
    journeys = _journey_ids(root)
    for row in rows:
        capability_id = row["ID"]
        qualification = row["Qualification"]
        if _QUALIFICATION.fullmatch(qualification) is None:
            raise ValueError(f"{capability_id}: invalid Qualification {qualification!r}")
        evidence = [item.strip() for item in row["Evidence"].split(",") if item.strip()]
        for item in evidence:
            if _PR.fullmatch(item) or _RUN.fullmatch(item):
                continue
            test = _TEST.fullmatch(item)
            if test is not None:
                path = root / test.group("path")
                if not path.is_file():
                    raise ValueError(
                        f"{capability_id}: test file does not exist: {test.group('path')}"
                    )
                definition = re.compile(rf"^def {re.escape(test.group('name'))}\s*\(", re.MULTILINE)
                if definition.search(path.read_text(encoding="utf-8")) is None:
                    raise ValueError(f"{capability_id}: test function does not exist: {item}")
                continue
            if _JOURNEY.fullmatch(item):
                if item not in journeys:
                    raise ValueError(f"{capability_id}: unknown acceptance journey: {item}")
                continue
            raise ValueError(f"{capability_id}: invalid evidence reference: {item}")
        if (qualification == "real-model-ci" or qualification.startswith("hardware:")) and not any(
            _PR.fullmatch(item) or _RUN.fullmatch(item) for item in evidence
        ):
            raise ValueError(f"{capability_id}: {qualification} requires PR# or run: evidence")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
    return completed.stdout.strip()


def check(root: Path, *, target: str = "HEAD", max_behind: int = 5) -> int:
    if max_behind < 0:
        raise ValueError("max_behind must be non-negative")
    state_text = (root / "CURRENT_STATE.md").read_text(encoding="utf-8")
    recorded = recorded_sha(state_text)
    validate_ledger(root, state_text)
    resolved_target = _git(root, "rev-parse", target)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", recorded, resolved_target],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if ancestor.returncode == 1:
        raise RuntimeError(f"CURRENT_STATE reconciliation {recorded} is not an ancestor of {resolved_target}")
    if ancestor.returncode != 0:
        raise RuntimeError(ancestor.stderr.strip() or "could not validate reconciliation ancestry")
    behind = int(_git(root, "rev-list", "--first-parent", "--count", f"{recorded}..{resolved_target}"))
    if behind > max_behind:
        raise RuntimeError(
            f"CURRENT_STATE.md is {behind} first-parent commits behind {resolved_target}; "
            f"allowed drift is {max_behind}. Reconcile the document against current main."
        )
    return behind


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail when CURRENT_STATE.md drifts too far from source.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--max-behind", type=int, default=5)
    args = parser.parse_args()
    behind = check(args.root.resolve(), target=args.target, max_behind=args.max_behind)
    print(f"CURRENT_STATE reconciliation is {behind} first-parent commit(s) behind {args.target}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
