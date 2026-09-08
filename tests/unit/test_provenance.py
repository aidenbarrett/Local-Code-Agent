"""Regression tests for experiment provenance."""

from __future__ import annotations

from pathlib import Path

from local_agent import provenance


REPO = Path(__file__).resolve().parent.parent.parent


def test_source_provenance_includes_all_skill_files():
    """Every regular skill file can affect treatment and must move source_sha256."""
    hashed = {path.resolve() for path in provenance._files()}
    skill_files = {
        path.resolve()
        for path in (REPO / "skills").rglob("*")
        if path.is_file() and not provenance._SKIP.intersection(path.relative_to(REPO).parts)
    }

    assert skill_files <= hashed, sorted(str(path.relative_to(REPO)) for path in skill_files - hashed)
