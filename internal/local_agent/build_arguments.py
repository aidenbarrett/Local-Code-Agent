"""Shared interpretation of build arguments at execution and proof boundaries."""

from __future__ import annotations


def is_valid_build_target(value: object) -> bool:
    """Whether *value* names one target without command syntax or whitespace."""
    return (
        isinstance(value, str)
        and bool(value)
        and value.replace("_", "").replace("-", "").replace(".", "").isalnum()
    )
