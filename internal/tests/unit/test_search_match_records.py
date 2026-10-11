"""Machine-delimited repository search must preserve paths and backend failures."""

from pathlib import Path
import shutil
from subprocess import run
from types import SimpleNamespace

import pytest

from local_agent.tools import search
from local_agent.tools.tool_context import ToolContext
from local_agent.tools.tool_primitives import ToolError, ToolRegistry


@pytest.mark.parametrize("separator", [":", "\0"])
def test_match_records_preserve_drive_letters_colons_and_newlines(separator):
    names = [r"C:\src\repo\ring.cpp", "source:12:file.cpp", "source\nfile.cpp"]
    records = "".join(f"{name}\0{29}{separator}bool RingBuffer::full() {{}}\n" for name in names)
    matches = search._parse_matches(records, 20, separator)
    assert [match["file"] for match in matches] == names
    assert all(match["line"] == 29 for match in matches)


@pytest.mark.parametrize("record", ["file:29:text\n", "file\0no-line:text\n", "\0" + "29:text\n"])
def test_malformed_match_records_refuse_instead_of_claiming_no_matches(record):
    with pytest.raises(ToolError, match="malformed match records"):
        search._parse_matches(record, 20, ":")


def _registry(root):
    registry = ToolRegistry()
    search.register(registry, ToolContext(repo=SimpleNamespace(root=root)))
    return registry


@pytest.mark.parametrize("use_rg", [True, False])
def test_search_backend_failure_never_becomes_an_empty_success(tmp_path, monkeypatch, use_rg):
    monkeypatch.setattr(search, "_rg_available", lambda: use_rg)
    backend = "_run_rg" if use_rg else "_run_git_grep"
    monkeypatch.setattr(search, backend, lambda *_args, **_kwargs: (2, "backend failed"))
    with pytest.raises(ToolError, match="backend failed"):
        _registry(tmp_path).get("search_text").handler(pattern="token")


def test_absolute_ripgrep_match_is_a_portable_repo_relative_definition(tmp_path, monkeypatch):
    source = tmp_path / "src" / "ring.cpp"
    source.parent.mkdir()
    source.write_text("bool RingBuffer::full() const { return true; }\n", encoding="utf-8")
    monkeypatch.setattr(search, "_rg_available", lambda: True)

    def backend(args, *_args, **_kwargs):
        assert "--null" in args
        return 0, f"{source}\0" + "1:bool RingBuffer::full() const { return true; }\n"

    monkeypatch.setattr(search, "_run_rg", backend)
    result = _registry(tmp_path).get("find_definition").handler(symbol="RingBuffer::full")
    assert result.data["matches"] == [
        {"file": "src/ring.cpp", "line": 1, "text": "bool RingBuffer::full() const { return true; }"}
    ]


@pytest.mark.parametrize("use_rg", [True, False])
def test_native_search_preserves_leading_dash_patterns(tmp_path, monkeypatch, use_rg):
    if use_rg and shutil.which("rg") is None:
        pytest.skip("ripgrep is unavailable")
    run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "source.cpp"
    source.write_text("// -needle: source text\n", encoding="utf-8")
    monkeypatch.setattr(search, "_rg_available", lambda: use_rg)
    result = _registry(tmp_path).get("search_text").handler(pattern="-needle")
    assert result.ok
    assert result.data["matches"] == [
        {"file": "source.cpp", "line": 1, "text": "// -needle: source text"}
    ]


@pytest.mark.parametrize("use_rg", [True, False])
def test_native_search_of_one_file_keeps_filename_and_scope(tmp_path, monkeypatch, use_rg):
    if use_rg and shutil.which("rg") is None:
        pytest.skip("ripgrep is unavailable")
    run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "README.md"
    source.write_text("cxxopts command line parser\n", encoding="utf-8")
    (tmp_path / "other.md").write_text("cxxopts unrelated match\n", encoding="utf-8")
    monkeypatch.setattr(search, "_rg_available", lambda: use_rg)
    result = _registry(tmp_path).get("search_text").handler(pattern="cxxopts", path="README.md")
    assert result.ok
    assert result.data["matches"] == [
        {"file": "README.md", "line": 1, "text": "cxxopts command line parser"}
    ]
