"""Typed validation shared by untrusted session boundaries."""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping


def require_integer(value: object, *, minimum: int | None, message: str) -> int:
    """Validate an integer bound without accepting bool as an execution identity."""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (minimum is not None and value < minimum)
    ):
        raise ValueError(message)
    return value


def require_nonempty_string(value: object, *, message: str) -> str:
    """Validate text without normalising the identity it represents."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def require_sha256(value: object, *, message: str) -> str:
    """Validate one lowercase SHA-256 digest without coercing boundary input."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(message)
    return value


def require_nonnegative_number(value: object, *, message: str) -> int | float:
    """Validate a nonnegative real timeout while refusing bool as a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
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
