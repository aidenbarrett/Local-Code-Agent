from __future__ import annotations

from pathlib import Path
import warnings


ROOT = Path(__file__).resolve().parents[2]
EXCLUDED_TOP_LEVEL = {
    ".git",
    ".pytest_cache",
    ".venv",
    ".venv-workstation",
    "experiments",  # frozen historical artifacts are not current executable source
}


def _active_python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts and relative.parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        files.append(path)
    return sorted(files)


def test_active_python_sources_compile_without_syntax_warnings() -> None:
    """Treat SyntaxWarning as an error across the active Python tree.

    This catches invalid Windows-path escape sequences and similar warning noise
    before it reaches a user-facing terminal or demo.
    """
    failures: list[str] = []

    for path in _active_python_files():
        source = path.read_text(encoding="utf-8")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                compile(source, str(path), "exec")
        except (SyntaxError, SyntaxWarning) as exc:
            relative = path.relative_to(ROOT)
            failures.append(f"{relative}: {exc}")

    assert not failures, "Python warning-cleanliness failures:\n" + "\n".join(failures)
