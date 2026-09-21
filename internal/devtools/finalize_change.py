#!/usr/bin/env python3
"""Verify repository provenance and frozen experiment contract axes.

`source_sha256` is derived from the exact working tree and is never stamped into a
shared repository file. That keeps provenance exact without making every source PR
edit the same JSON line. `internal/INSTRUMENT.json` declares only the frozen
model-facing and outcome-facing axes; drift on either still fails closed and requires
an explicit generation/methodology decision.
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
_HEX = frozenset("0123456789abcdef")


class FinalizationError(RuntimeError):
    """The current tree cannot be safely checked."""


def compute_identities() -> dict[str, str]:
    """Compute source provenance plus both frozen contract identities."""
    internal_text = str(INTERNAL)
    if internal_text not in sys.path:
        sys.path.insert(0, internal_text)
    from local_agent import provenance

    return {
        "source_sha256": provenance.source_sha256(),
        "base_prompt_sha256": provenance.base_prompt_sha256(),
        "outcome_contract_sha256": provenance.outcome_contract_sha256(),
    }


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX for ch in value)
    )


def load_declaration(path: Path = INSTRUMENT) -> dict[str, Any]:
    """Load and validate only the values that are actually declarations."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"cannot read a valid identity declaration: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError("identity declaration must be a JSON object")
    if "source_sha256" in value:
        raise FinalizationError(
            "source_sha256 must be derived from the exact tree, not committed as a live declaration"
        )
    invalid = [key for key in CONTRACT_KEYS if not _valid_sha256(value.get(key))]
    if invalid:
        raise FinalizationError(
            "identity declaration has missing or invalid frozen SHA-256 fields: "
            + ", ".join(invalid)
        )
    return value


def contract_drift(
    declared: dict[str, Any], actual: dict[str, str]
) -> dict[str, tuple[Any, str]]:
    return {
        key: (declared.get(key), actual[key])
        for key in CONTRACT_KEYS
        if declared.get(key) != actual[key]
    }


def _print_identities(actual: dict[str, str]) -> None:
    for key in IDENTITY_KEYS:
        print(f"{key:<24} {actual[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute exact source provenance and verify frozen contract axes."
    )
    parser.add_argument(
        "--write-source",
        action="store_true",
        help=(
            "deprecated compatibility flag; source_sha256 is now derived per tree "
            "and no repository file is written"
        ),
    )
    args = parser.parse_args(argv)

    try:
        declared = load_declaration()
        actual = compute_identities()
    except (FinalizationError, OSError, ImportError, ValueError, KeyError) as exc:
        print(f"REFUSED: identity verification unavailable: {exc}", file=sys.stderr)
        return 3

    _print_identities(actual)
    drift = contract_drift(declared, actual)
    if drift:
        print("\nREFUSED: a frozen contract axis moved.")
        for key, (want, got) in drift.items():
            print(f"  {key}\n    declared  {want}\n    computed  {got}")
        print(
            "Do not auto-update this declaration. Stop for an explicit "
            "generation/methodology decision."
        )
        return 2

    if args.write_source:
        print(
            "\n--write-source is no longer necessary: source_sha256 is derived "
            "from the exact tree and recorded with run provenance."
        )
    print("\nFrozen contract axes match. Source provenance is derived and needs no stamp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
