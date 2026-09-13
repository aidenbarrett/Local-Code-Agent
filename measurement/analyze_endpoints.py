#!/usr/bin/env python3
"""Analyse generation-2 pilot rows under the pre-registered endpoint policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from evaluation.endpoints import analyse  # noqa: E402


_IDENTITY_KEYS = (
    "generation",
    "source_sha256",
    "base_prompt_sha256",
    "outcome_contract_sha256",
    "model",
    "model_identity",
    "rehearsal",
)


def _identity(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    missing = [key for key in _IDENTITY_KEYS if key not in payload]
    if missing:
        raise ValueError(f"{path}: missing analysis identity fields: {missing}")

    generation = payload["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValueError(f"{path}: generation must be an integer")

    for key in ("source_sha256", "base_prompt_sha256", "outcome_contract_sha256"):
        value = payload[key]
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"{path}: malformed {key}")

    if not isinstance(payload["model"], str) or not payload["model"]:
        raise ValueError(f"{path}: missing model")
    if not isinstance(payload["model_identity"], dict) or not payload["model_identity"]:
        raise ValueError(f"{path}: missing model configuration")
    if not isinstance(payload["rehearsal"], bool):
        raise ValueError(f"{path}: rehearsal must be boolean")
    if payload["rehearsal"]:
        raise ValueError(f"{path}: rehearsal data cannot enter scored analysis")

    return {key: payload[key] for key in _IDENTITY_KEYS}


def _payload(
    path: Path,
) -> tuple[list[dict], dict[tuple[str, str], int], dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a result object")
    if payload.get("complete") is not True:
        raise ValueError(f"{path}: run is not complete")

    rows = payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected an object containing a rows array")

    condition = payload.get("condition")
    declared = payload.get("attempts_declared")
    if not isinstance(condition, str) or not isinstance(declared, dict):
        raise ValueError(
            f"{path}: missing condition/attempts_declared evidence; "
            "attempt completeness cannot be verified"
        )

    identity = _identity(payload, path)
    for index, row in enumerate(rows):
        if row.get("condition") != condition:
            raise ValueError(
                f"{path}: row {index} condition {row.get('condition')!r} "
                f"does not match payload {condition!r}"
            )
        for key, value in identity.items():
            if row.get(key) != value:
                raise ValueError(
                    f"{path}: row {index} identity mismatch for {key}: "
                    f"{row.get(key)!r} != {value!r}"
                )

    out: dict[tuple[str, str], int] = {}
    for case, count in declared.items():
        if not isinstance(case, str) or not isinstance(count, int) or isinstance(count, bool):
            raise ValueError(f"{path}: malformed attempts_declared entry {case!r}: {count!r}")
        out[(condition, case)] = count
    return rows, out, identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the frozen four-endpoint and 2-of-3 repeat policy."
    )
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, help="also write the analysis as JSON")
    args = parser.parse_args(argv)

    rows: list[dict] = []
    attempts_declared: dict[tuple[str, str], int] = {}
    expected_identity: dict[str, Any] | None = None

    for path in args.results:
        file_rows, file_declared, file_identity = _payload(path)
        if expected_identity is None:
            expected_identity = file_identity
        elif file_identity != expected_identity:
            raise ValueError(
                f"{path}: mixed generation/model/instrument identity in one analysis"
            )

        overlap = set(attempts_declared).intersection(file_declared)
        if overlap:
            raise ValueError(f"duplicate attempt declarations across files: {sorted(overlap)}")
        rows.extend(file_rows)
        attempts_declared.update(file_declared)

    result = analyse(rows, attempts_declared)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
