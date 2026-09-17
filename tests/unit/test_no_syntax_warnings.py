"""Every Python file in the repository must compile without a syntax warning.

Written after `scripts/chat.py` shipped with `.\\chat.ps1` inside a plain
docstring. `\\c` is not a valid escape sequence, so Python emitted a warning on
every single launch, printed above the banner, in front of whoever was being
shown the tool. It was invisible on Python 3.11, where invalid escapes are a
DeprecationWarning and hidden by default, and loud on 3.12+, where they are a
SyntaxWarning and shown.

That asymmetry is the reason this test exists rather than a habit: the machine
that writes the code and the machine that demonstrates it disagree about
whether the defect is visible.

The check is on the warning, not on the escape, so it also catches the other
things `compile()` warns about, such as `is` against a literal or a comparison
that is always true.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Generated, vendored or transient trees. Nothing here is ours to keep clean.
SKIP = {"__pycache__", ".git", "build", ".venv", ".venv-workstation",
        ".pytest_cache", ".mypy_cache", "node_modules"}


def _sources() -> list[Path]:
    return sorted(
        p for p in REPO.rglob("*.py") if not SKIP.intersection(p.parts)
    )


def test_there_are_sources_to_check() -> None:
    """A glob that silently matches nothing would make this suite vacuous."""
    assert len(_sources()) > 50


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.name))
def test_compiles_without_warning(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compile(source, str(path), "exec")

    # An invalid escape is a DeprecationWarning on 3.11 and a SyntaxWarning on
    # 3.12+, so match on either, and on the message for the escape case.
    complaints = [
        f"line {w.lineno}: {w.category.__name__}: {w.message}"
        for w in caught
        if issubclass(w.category, SyntaxWarning) or "escape" in str(w.message)
    ]
    relative = path.relative_to(REPO).as_posix()
    assert not complaints, (
        f"{relative} does not compile cleanly:\n  "
        + "\n  ".join(complaints)
        + "\n\nFor a Windows path or a regex in a docstring or literal, make it "
          "a raw string (r\"\"\"...\"\"\") or double the backslash."
    )
