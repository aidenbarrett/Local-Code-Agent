"""Scalar validation shared by cancellation and endpoint authority boundaries."""
from __future__ import annotations


def require_integer(value: object, *, minimum: int, message: str) -> int:
    """Validate an integer bound without accepting bool as an execution identity."""
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(message)
    return value


def require_nonempty_string(value: object, *, message: str) -> str:
    """Validate text without normalising the identity it represents."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value
