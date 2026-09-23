from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_agent.tools.files import _iter_files, _read_text_window
from local_agent.tools.tool_primitives import ToolError


def test_recursive_enumeration_prunes_managed_environment_before_descent(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
    blocked = tmp_path / ".venv-workstation"
    (blocked / "Lib" / "site-packages").mkdir(parents=True)
    (blocked / "Lib" / "site-packages" / "huge.py").write_text("ignored\n", encoding="utf-8")

    visited: list[Path] = []
    real_scandir = os.scandir

    def tracking_scandir(path):
        visited.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", tracking_scandir)
    files = list(_iter_files(tmp_path, recursive=True))

    assert [path.relative_to(tmp_path).as_posix() for path in files] == ["src/main.cpp"]
    assert blocked not in visited
    assert blocked / "Lib" not in visited


def test_explicit_range_can_read_large_file_without_whole_file_materialisation(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text("".join(f"line {index}\n" for index in range(10_000)), encoding="utf-8")
    assert target.stat().st_size > 128

    window, start, total = _read_text_window(
        target,
        start_line=10,
        end_line=12,
        max_read_bytes=128,
    )

    assert start == 10
    assert window == ["line 9", "line 10", "line 11"]
    assert total is None


def test_large_whole_file_read_still_fails_closed(tmp_path):
    target = tmp_path / "large.txt"
    target.write_text("x" * 1024, encoding="utf-8")

    with pytest.raises(ToolError, match="explicit line range"):
        _read_text_window(target, start_line=1, end_line=None, max_read_bytes=128)


def test_line_ranges_are_explicitly_bounded(tmp_path):
    target = tmp_path / "source.cpp"
    target.write_text("x\n" * 1000, encoding="utf-8")

    with pytest.raises(ToolError, match="at most 400 lines"):
        _read_text_window(target, start_line=1, end_line=401, max_read_bytes=4096)
