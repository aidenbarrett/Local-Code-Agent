"""Regression coverage for the no-ripgrep repository-search fallback."""

from pathlib import Path
from subprocess import run
from types import SimpleNamespace

from local_agent.tools import search
from local_agent.tools.tool_context import ToolContext
from local_agent.tools.tool_primitives import ToolRegistry


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_git_grep_fallback_honours_path_and_excludes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(search, "_rg_available", lambda: False)

    run(["git", "init", "-q"], cwd=repo, check=True)
    _write(repo / "src" / "tracked.cpp", "fallback_scope_token\n")
    _write(repo / "src" / "new.cpp", "fallback_scope_token\n")
    _write(repo / "docs" / "outside.md", "fallback_scope_token\n")
    _write(repo / "build" / "generated.cpp", "fallback_scope_token\n")
    _write(repo / "cmake-build-debug" / "generated.cpp", "fallback_scope_token\n")
    _write(repo / "src" / "build" / "nested.cpp", "fallback_scope_token\n")
    run(["git", "add", "src/tracked.cpp", "docs/outside.md", "build/generated.cpp",
         "cmake-build-debug/generated.cpp", "src/build/nested.cpp"], cwd=repo, check=True)

    registry = ToolRegistry()
    search.register(registry, ToolContext(repo=SimpleNamespace(root=repo)))
    scoped = registry.get("search_text").handler(
        pattern="fallback_scope_token", path="src",
    )
    whole_repo = registry.get("search_text").handler(pattern="fallback_scope_token")

    assert scoped.ok
    assert [match["file"] for match in scoped.data["matches"]] == [
        "src/new.cpp", "src/tracked.cpp",
    ]
    assert [match["file"] for match in whole_repo.data["matches"]] == [
        "docs/outside.md", "src/new.cpp", "src/tracked.cpp",
    ]
