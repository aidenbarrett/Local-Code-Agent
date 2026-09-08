from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.tools.base import SandboxError, ToolResult, resolve_in_repo


def test_relative_path_resolves(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cpp").write_text("int main(){}")
    assert resolve_in_repo(tmp_path, "src/a.cpp").name == "a.cpp"


@pytest.mark.parametrize(
    "attempt",
    [
        "../../../../etc/passwd",
        "../outside.txt",
        "/etc/passwd",
        "src/../../escape",
    ],
)
def test_escape_blocked(tmp_path: Path, attempt: str):
    (tmp_path / "src").mkdir()
    with pytest.raises(SandboxError):
        resolve_in_repo(tmp_path, attempt)


def test_symlink_escape_blocked(tmp_path: Path):
    # Outside the repo root, but inside this test's own directory. The first
    # version wrote to tmp_path.parent, a shared location, and only passed as
    # root: as a user it collided with a file another run had left behind.
    outside = tmp_path / "elsewhere" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("private")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxError):
        resolve_in_repo(root, "link")


def test_tool_result_truncates_over_budget():
    result = ToolResult(
        ok=False,
        summary="build failed",
        data={"blob": "x" * 50_000},
        artifacts=["runs/1/combined.log"],
    )
    payload = result.to_json(max_bytes=2000)
    assert len(payload.encode()) <= 2000
    assert "truncated" in payload
    assert "runs/1/combined.log" in payload


def test_tool_result_keeps_small_payload():
    result = ToolResult(ok=True, summary="fine", data={"n": 1})
    assert '"n": 1' in result.to_json()
