"""Bounded file-request grammar and pre-admission repository boundary.

This is not a general natural-language parser. Unsupported phrasing gains no
deterministic mutation authority. Path containment reuses the tool sandbox; the
worker must still satisfy that sandbox for every effect in its candidate tree.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
import re
import sys
from typing import Final

from ..tools.tool_primitives import SandboxError, resolve_in_repo

_DIRECT_CHANGE: Final = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:create|add|edit|modify|update)\s+(?P<body>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_CHANGE: Final = re.compile(r"^(?:/change|change:)\s+", re.IGNORECASE)
_FILE_PREFIX: Final = re.compile(
    r"^(?:(?:a|an|the)\s+)?(?:new\s+)?"
    r"(?:(?:C\+\+|C|Python|header|source|text)\s+)?file\s+"
    r"(?:(?:for\s+me|please|new)\s+)*(?:(?:called|named)\s+)?",
    re.IGNORECASE,
)
_TOKEN: Final = re.compile(r'''`([^`\n]+)`|"([^"\n]+)"|'([^'\n]+)'|([^\s`"']+)''')
_PROHIBITED: Final = (
    (re.compile(r"\bpush\b", re.IGNORECASE), "push_not_supported"),
    (re.compile(r"\bcommit\b", re.IGNORECASE), "commit_needs_candidate"),
    (re.compile(r"\bgit\b|\bsubmodules?\b", re.IGNORECASE), "git_change_not_supported"),
    (re.compile(r"\b(?:delete|remove|rm)\b", re.IGNORECASE), "change_needs_prefix"),
)


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(
        next(group for group in match.groups() if group is not None).rstrip(".,!?;:")
        for match in _TOKEN.finditer(text)
    )


def _path_shaped(token: str) -> bool:
    return "/" in token or "\\" in token or bool(re.search(r"\.[\w-]+$", token))


def natural_change_target(text: str) -> str | None:
    """Only a direct verb followed by a concrete file target grants this route."""
    match = _DIRECT_CHANGE.fullmatch(text.strip())
    if match is None:
        return None
    body = _FILE_PREFIX.sub("", match.group("body"), count=1)
    body = re.sub(r"^the\s+", "", body, count=1, flags=re.IGNORECASE)
    tokens = _tokens(body)
    return tokens[0] if tokens and _path_shaped(tokens[0]) else None


def _path_refusal(token: str, root: Path) -> str | None:
    # A Windows drive/UNC path on POSIX must never become a relative filename.
    # Drive-relative paths and home/environment expansion are also not guessed.
    windows = PureWindowsPath(token)
    if ((windows.drive and (sys.platform != "win32" or not windows.is_absolute()))
            or token.startswith(("~", "$", "%"))):
        return "outside_active_repository"
    try:
        resolve_in_repo(root, token.replace("\\", "/"))
    except SandboxError:
        return "outside_active_repository"
    except (OSError, RuntimeError, ValueError):
        return "unresolved_change_path"
    return None


def change_request_refusal(text: str) -> str | None:
    """Unsupported clauses win throughout a direct request, before file routing.

    Explicit change commands retain source-deletion authority, but gain no Git
    authority. Paths such as git.cpp are not treated as operation keywords.
    """
    stripped = text.strip()
    natural = _DIRECT_CHANGE.fullmatch(stripped)
    explicit = _EXPLICIT_CHANGE.match(stripped)
    if natural is None and explicit is None:
        return None
    # A filename such as git.cpp is a target, not a Git operation.
    words = " ".join(token for token in _tokens(stripped) if not _path_shaped(token))
    for pattern, reason in _PROHIBITED:
        if reason == "change_needs_prefix" and explicit is not None:
            continue
        if pattern.search(words):
            return reason
    return None


def change_path_refusal(text: str, root: Path | None) -> str | None:
    """Resolve named paths before model proposals, admission and resumed work.

    Check all path-shaped tokens, including later destination clauses. This bounded
    grammar can conservatively refuse paths in examples; it never substitutes a
    repo-local destination for an external path. Tool checks still own each effect.
    """
    stripped = text.strip()
    if _DIRECT_CHANGE.fullmatch(stripped) is None and _EXPLICIT_CHANGE.match(stripped) is None:
        return None
    tokens = tuple(token for token in _tokens(stripped) if _path_shaped(token))
    if tokens and root is None:
        return "active_repository_unknown"
    if root is not None:
        for token in tokens:
            reason = _path_refusal(token, root)
            if reason is not None:
                return reason
    return None
