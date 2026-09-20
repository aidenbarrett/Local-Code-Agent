#!/usr/bin/env python3
"""Finalize repository identity before a branch is pushed.

This is intentionally a developer tool, not part of the measured agent surface.
It may auto-stamp source-only drift, but it never updates model-facing or
outcome-facing contract identities. Those require an explicit generation
methodology decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = ROOT / "internal"
INSTRUMENT = INTERNAL / "INSTRUMENT.json"
IDENTITY_KEYS = (
    "source_sha256",
    "base_prompt_sha256",
    "outcome_contract_sha256",
)
CONTRACT_KEYS = (
    "base_prompt_sha256",
    "outcome_contract_sha256",
)


def compute_identities() -> dict[str, str]:
    """Compute all three identities from the current working tree."""
    internal_text = str(INTERNAL)
    if internal_text not in sys.path:
        sys.path.insert(0, internal_text)
    from local_agent import provenance

    return {
        "source_sha256": provenance.source_sha256(),
        "base_prompt_sha256": provenance.base_prompt_sha256(),
        "outcome_contract_sha256": provenance.outcome_contract_sha256(),
    }


def load_declaration(path: Path = INSTRUMENT) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def identity_drift(
    declared: dict[str, Any], actual: dict[str, str]
) -> dict[str, tuple[Any, str]]:
    return {
        key: (declared.get(key), actual[key])
        for key in IDENTITY_KEYS
        if declared.get(key) != actual[key]
    }


def stamp_source(
    path: Path, declared: dict[str, Any], source_sha256: str
) -> None:
    """Atomically update only the source identity declaration."""
    updated = dict(declared)
    updated["source_sha256"] = source_sha256
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _print_identities(actual: dict[str, str]) -> None:
    for key in IDENTITY_KEYS:
        print(f"{key:<24} {actual[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify repository identities and optionally stamp source-only drift."
    )
    parser.add_argument(
        "--write-source",
        action="store_true",
        help=(
            "update internal/INSTRUMENT.json when source_sha256 is the only drift; "
            "contract-axis drift is always refused"
        ),
    )
    args = parser.parse_args(argv)

    declared = load_declaration()
    actual = compute_identities()
    drift = identity_drift(declared, actual)

    _print_identities(actual)

    contract_drift = {key: drift[key] for key in CONTRACT_KEYS if key in drift}
    if contract_drift:
        print("\nREFUSED: a frozen contract axis moved.")
        for key, (want, got) in contract_drift.items():
            print(f"  {key}\n    declared  {want}\n    computed  {got}")
        print(
            "Do not auto-stamp this change. Stop for an explicit generation/methodology decision."
        )
        return 2

    source_drift = drift.get("source_sha256")
    if source_drift is None:
        print("\nIdentity declaration matches this tree.")
        return 0

    want, got = source_drift
    if not args.write_source:
        print("\nSOURCE IDENTITY DRIFT")
        print(f"  declared  {want}\n  computed  {got}")
        print("Run again with --write-source before pushing this branch.")
        return 1

    stamp_source(INSTRUMENT, declared, got)
    print("\nStamped source_sha256 in internal/INSTRUMENT.json.")
    print("Frozen contract axes were unchanged. Commit the declaration with this change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
