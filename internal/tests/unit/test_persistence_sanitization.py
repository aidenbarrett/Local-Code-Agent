from __future__ import annotations

import re

from local_agent.persistence import sanitize_for_persistence

_POSIX = re.compile(r"(?<![.:/A-Za-z0-9])/(?!/)[^\s\"'<>|]+")
_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+")
_UNC = re.compile(r"(?<!\\)\\\\[^\\/\s]+[\\/][^\s\"'<>|]+")


def _absolute_paths(value):
    found = []
    if isinstance(value, str):
        for regex in (_POSIX, _DRIVE, _UNC):
            found.extend(match.group(0) for match in regex.finditer(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_absolute_paths(key))
            found.extend(_absolute_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_absolute_paths(item))
    return found


def test_persistence_sanitizer_removes_absolute_paths_recursively():
    payload = {
        "/home/user/key": [
            "prefix /home/user/project/private.cpp suffix",
            {"windows": r"C:\Users\User\work\asset.bin"},
            {"unc": r"\\host\share\secret\model.bin"},
        ]
    }
    safe = sanitize_for_persistence(payload)
    assert _absolute_paths(safe) == []


def test_parent_relative_reference_is_not_rewritten_as_an_absolute_path():
    value = "../../tmp/private/run.json"
    assert sanitize_for_persistence(value) == value
    assert _absolute_paths(value) == []
