"""Shared syntax for bounded C++ symbol lookup.

The router and repository tools must agree about which spellings are supported.
Template-ids are deliberately not normalised: erasing template arguments can turn a
precise request into a different symbol, so they remain a typed limitation until a
language-aware owner exists.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_COMPONENT = re.compile(rf"~?{_IDENTIFIER}")


@dataclass(frozen=True)
class CppSymbol:
    """A validated, non-template C++ qualified identifier."""

    text: str
    components: tuple[str, ...]


def cpp_symbol_rejection(symbol: str) -> str | None:
    """Return the stable reason a public symbol spelling is unsupported."""
    if any(char in symbol for char in "<>"):
        return "cpp_template_symbol_unsupported"
    parts = tuple(symbol.split("::"))
    if not parts or any(not _COMPONENT.fullmatch(part) for part in parts):
        return "invalid_cpp_symbol"
    destructors = [index for index, part in enumerate(parts) if part.startswith("~")]
    if destructors:
        if destructors != [len(parts) - 1]:
            return "invalid_cpp_symbol"
        destroyed = parts[-1][1:]
        if len(parts) > 1 and parts[-2] != destroyed:
            return "invalid_cpp_symbol"
    return None


def parse_cpp_symbol(symbol: str) -> CppSymbol:
    """Validate one supported symbol spelling or raise with its stable reason."""
    rejection = cpp_symbol_rejection(symbol)
    if rejection is not None:
        raise ValueError(rejection)
    return CppSymbol(symbol, tuple(symbol.split("::")))
