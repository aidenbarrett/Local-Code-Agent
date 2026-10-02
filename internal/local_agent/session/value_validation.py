"""Typed validation shared by untrusted session boundaries."""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping


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


def require_string_tuple(values: object, *, message: str) -> tuple[str, ...]:
    """Copy an iterable of nonempty strings into an immutable typed value."""
    if not isinstance(values, Iterable):
        raise ValueError(message)
    checked: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(message)
        checked.append(value)
    return tuple(checked)


def require_exact_keys(
    value: object,
    required: Collection[str],
    *,
    message: str,
) -> Mapping[str, object]:
    """Return a mapping only when its keys exactly match the boundary contract."""
    if not isinstance(value, Mapping) or set(value) != set(required):
        raise ValueError(message)
    return value
