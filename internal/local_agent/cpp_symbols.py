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


def user_symbol_spelling(raw: str) -> str:
    """The symbol inside how people write it in a question.

    ```Widget```, ``"Widget"`` and ``Widget::ready()`` all name a symbol; the quotes
    and a trailing empty call are presentation, not part of the identifier.
    """
    text = raw.strip()
    for quote in ("`", '"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
            break
    if text.endswith("()"):
        text = text[:-2].rstrip()
    return text


def cpp_symbol_rejection(symbol: str) -> str | None:
    """Return the stable reason a public symbol spelling is unsupported.

    Presentation (quotes, a trailing empty call) is removed first, here, so the
    router and every tool judge exactly the same canonical spelling.
    """
    symbol = user_symbol_spelling(symbol)
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
    canonical = user_symbol_spelling(symbol)
    return CppSymbol(canonical, tuple(canonical.split("::")))
