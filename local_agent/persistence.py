"""Privacy-safe persistence helpers for experiment evidence.

Persisted rows and manifests must not contain host-specific absolute filesystem
paths. They can expose usernames, machine layout and internal asset names while
adding no scientific value. Redaction happens at the persistence boundary over
the complete structure so newly-added fields inherit the same rule.
"""

from __future__ import annotations

import hashlib
import os
import re
from functools import wraps
from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

# The patterns intentionally identify path prefixes rather than trying to parse
# every legal filesystem character. Stopping at whitespace is conservative for
# privacy: even a path containing spaces loses the absolute/user-specific root.
_ABSOLUTE_PATH = re.compile(
    r"(?P<unc>(?<!\\)\\\\[^\\/\s]+[\\/][^\s\"'<>|]+)"
    r"|(?P<drive>(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+)"
    r"|(?P<posix>(?<![.:/A-Za-z0-9])/(?!/)[^\s\"'<>|]+)"
)


def _token(path_text: str) -> str:
    digest = hashlib.sha256(path_text.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"<abs-path:{digest}>"


def redact_absolute_paths(text: str) -> str:
    """Replace absolute filesystem paths embedded anywhere in *text*."""
    return _ABSOLUTE_PATH.sub(lambda match: _token(match.group(0)), text)


def sanitize_for_persistence(value: Any) -> Any:
    """Recursively redact absolute paths from JSON-like evidence structures."""
    if isinstance(value, str):
        return redact_absolute_paths(value)
    if isinstance(value, os.PathLike):
        return redact_absolute_paths(os.fspath(value))
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = sanitize_for_persistence(key) if isinstance(key, (str, os.PathLike)) else key
            if safe_key in out and safe_key != key:
                raise ValueError("path redaction caused a duplicate persistence key")
            out[safe_key] = sanitize_for_persistence(item)
        return out
    if isinstance(value, list):
        return [sanitize_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_persistence(item) for item in value)
    return value


def sanitized_result(fn: _F) -> _F:
    """Wrap a producer so every returned evidence structure is privacy-safe."""
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return sanitize_for_persistence(fn(*args, **kwargs))

    return wrapped  # type: ignore[return-value]
