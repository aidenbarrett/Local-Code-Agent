from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.tools.tool_primitives import SandboxError, ToolResult, resolve_in_repo


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
        "..\\outside.txt",
        "src\\..\\..\\outside.txt",
        r"C:\\outside.txt",
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


def test_absolute_path_inside_repository_is_still_refused(tmp_path: Path):
    target = tmp_path / "inside.txt"
    target.write_text("inside", encoding="utf-8")
    with pytest.raises(SandboxError, match="absolute path"):
        resolve_in_repo(tmp_path, target)


@pytest.mark.parametrize("tool_name", ["read_file", "propose_patch"])
def test_file_tools_refuse_outside_symlink_without_reading_or_writing(loaded, tmp_path, tool_name):
    sandbox, _, registry, _, _ = loaded
    outside = tmp_path / "outside.cpp"
    outside.write_text("secret outside bytes\n", encoding="utf-8")
    link = sandbox.root / "src" / "outside-link.cpp"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    before = outside.read_bytes()
    handler = registry.get(tool_name).handler
    kwargs = {"path": "src/outside-link.cpp"}
    if tool_name == "propose_patch":
        kwargs.update(find="secret", replace="changed")
    with pytest.raises(SandboxError):
        handler(**kwargs)
    assert outside.read_bytes() == before


@pytest.mark.parametrize("path", ["../outside.cpp", "..\\outside.cpp"])
@pytest.mark.parametrize("tool_name", ["read_file", "propose_patch"])
def test_file_tools_refuse_cross_platform_traversal(loaded, path, tool_name):
    _, _, registry, _, _ = loaded
    handler = registry.get(tool_name).handler
    kwargs = {"path": path}
    if tool_name == "propose_patch":
        kwargs.update(find="secret", replace="changed")
    with pytest.raises(SandboxError):
        handler(**kwargs)


@pytest.mark.parametrize("tool_name", ["read_file", "propose_patch"])
def test_file_tools_refuse_absolute_paths_even_inside_repository(loaded, tool_name):
    sandbox, _, registry, _, _ = loaded
    path = str((sandbox.root / "src" / "ring_buffer.cpp").resolve())
    handler = registry.get(tool_name).handler
    kwargs = {"path": path}
    if tool_name == "propose_patch":
        kwargs.update(find="RingBuffer", replace="Changed")
    with pytest.raises(SandboxError, match="absolute path"):
        handler(**kwargs)


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
