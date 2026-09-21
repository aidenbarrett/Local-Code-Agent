from __future__ import annotations

from devtools.check_source_declaration_diff import declaration_error, hashed_source_changes


def _provenance(tmp_path):
    path = tmp_path / "provenance.py"
    path.write_text(
        "_HASHED = ((\"internal/local_agent\", \"*.py\"), (\"internal/docs/session-contract/v1\", \"*.json\"))\n"
        "_HASHED_FILES = (\"pyproject.toml\",)\n",
        encoding="utf-8",
    )
    return path


def test_hashed_surface_is_read_from_provenance_not_redeclared(tmp_path):
    provenance = _provenance(tmp_path)
    assert hashed_source_changes([
        "internal/local_agent/session/foo.py",
        "internal/local_agent/session/README.md",
        "internal/docs/session-contract/v1/task.json",
        "pyproject.toml",
        "internal/tests/unit/test_x.py",
    ], provenance_path=provenance) == (
        "internal/docs/session-contract/v1/task.json",
        "internal/local_agent/session/foo.py",
        "pyproject.toml",
    )


def test_hashed_change_requires_declaration_in_same_diff(tmp_path):
    provenance = _provenance(tmp_path)
    error = declaration_error(
        ["internal/local_agent/session/foo.py"],
        provenance_path=provenance,
    )
    assert error is not None
    assert "INSTRUMENT.json" in error[0]
    assert error[1] == ("internal/local_agent/session/foo.py",)


def test_declaration_satisfies_presence_gate_but_not_hash_correctness(tmp_path):
    provenance = _provenance(tmp_path)
    assert declaration_error([
        "internal/local_agent/session/foo.py",
        "internal/INSTRUMENT.json",
    ], provenance_path=provenance) is None


def test_non_hashed_changes_do_not_require_declaration(tmp_path):
    provenance = _provenance(tmp_path)
    assert declaration_error([
        ".github/workflows/check.yml",
        "internal/tests/unit/test_x.py",
        "CURRENT_STATE.md",
    ], provenance_path=provenance) is None
