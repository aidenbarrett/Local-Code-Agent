"""Read-only comparison of retained acceptance evidence, without running a model."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class ComparisonError(ValueError):
    """A report is missing, incompatible or malformed."""


@dataclass(frozen=True)
class Attempt:
    journey: str
    kind: str
    status: str
    peak: int | None


@dataclass(frozen=True)
class Report:
    directory: Path
    model: str
    attempts: tuple[Attempt, ...]


def read_report(directory: Path, schema: str) -> Report:
    path = directory / "journeys.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComparisonError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ComparisonError(f"{path}: expected schema {schema}")
    rows = data.get("journeys")
    if not isinstance(rows, list) or not rows:
        raise ComparisonError(f"{path}: journeys must be a non-empty list")
    attempts: list[Attempt] = []
    seen: set[str] = set()
    kinds: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ComparisonError(f"{path}: journey must be an object")
        identity, kind, status = row.get("id"), row.get("kind"), row.get("status")
        if (not isinstance(identity, str) or not identity or identity in seen
                or kind not in ("model", "product") or not isinstance(status, str)
                or not re.fullmatch(r"(?:PASS|FAIL|UNKNOWN|MEASURED:[a-z_]+)", status)):
            raise ComparisonError(f"{path}: invalid or duplicate journey {identity!r}")
        seen.add(identity)
        base = re.sub(r"\.r[1-9][0-9]*$", "", identity) if kind == "model" else identity
        if base in kinds and kinds[base] != kind:
            raise ComparisonError(f"{path}: inconsistent journey kind for {base}")
        kinds[base] = kind
        tasks = row.get("tasks", [])
        if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
            raise ComparisonError(f"{path}: {identity} tasks must be objects")
        peaks: list[int] = []
        for task in tasks:
            value = task.get("context_peak_tokens")
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ComparisonError(f"{path}: {identity} has invalid context peak")
                peaks.append(value)
        attempts.append(Attempt(base, kind, status, max(peaks) if peaks else None))
    model = data.get("model")
    return Report(directory, model if isinstance(model, str) else "unknown model", tuple(attempts))


def _outcomes(attempts: list[Attempt]) -> str:
    if not attempts:
        return "MISSING"
    counts = Counter(attempt.status.removeprefix("MEASURED:") for attempt in attempts)
    return ", ".join(f"{status} {count}/{len(attempts)}" for status, count in sorted(counts.items()))


def _peak(attempts: list[Attempt]) -> str:
    peaks = [attempt.peak for attempt in attempts if attempt.peak is not None]
    if not peaks:
        return "unknown"
    shown = f"{max(peaks):,}"
    return shown if len(peaks) == len(attempts) else shown + " (partial)"


def comparison_text(old_dir: Path, new_dir: Path, schema: str) -> str:
    old, new = read_report(old_dir, schema), read_report(new_dir, schema)
    groups: list[dict[str, list[Attempt]]] = []
    for report in (old, new):
        grouped: dict[str, list[Attempt]] = {}
        for attempt in report.attempts:
            grouped.setdefault(attempt.journey, []).append(attempt)
        groups.append(grouped)
    previous, current = groups
    lines = ["ACCEPTANCE RUN COMPARISON", f"Old: {old.directory} · {old.model}",
             f"New: {new.directory} · {new.model}", "",
             "Journey | Old outcomes | New outcomes | Old peak tokens | New peak tokens",
             "--- | --- | --- | --- | ---"]
    for name in sorted(previous.keys() | current.keys()):
        before, after = previous.get(name, []), current.get(name, [])
        if before and after and before[0].kind != after[0].kind:
            raise ComparisonError(f"journey {name} changed kind between reports")
        lines.append(f"{name} | {_outcomes(before)} | {_outcomes(after)} | "
                     f"{_peak(before)} | {_peak(after)}")
    lines += ["", "Product verdict totals (recorded checks)"]
    for label, report in (("Old", old), ("New", new)):
        counts = Counter(a.status for a in report.attempts if a.kind == "product")
        lines.append(f"{label}: " + " / ".join(f"{status} {counts[status]}"
                                               for status in ("PASS", "FAIL", "UNKNOWN")))
    lines.append("Counts describe these samples; unknown or missing evidence is not success.")
    return "\n".join(lines) + "\n"
