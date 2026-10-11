"""Patch application and owned rollback preserve exact UTF-8 file bytes."""
from pathlib import Path

import pytest

from local_agent.agent.journal import MutationJournal
from local_agent.config import Policy, RepoConfig
from local_agent.tools import build_registry
from local_agent.tools.tool_primitives import ToolError


@pytest.mark.parametrize("original", [
    b"before\nvalue = 1\nafter\n",
    b"before\r\nvalue = 1\r\nafter\r\n",
    b"before\r\nvalue = 1\nafter",
    b"\xef\xbb\xbfbefore\nvalue = 1\r\nafter",
])
def test_inline_patch_and_rollback_preserve_bytes(tmp_path: Path, original: bytes) -> None:
    path = tmp_path / "source.py"
    path.write_bytes(original)
    journal = MutationJournal()
    repo = RepoConfig(tmp_path, "fidelity", "build", "runs", {}, "default", Policy(allow_patch=True))
    registry, _, _ = build_registry(repo, journal=journal)
    proposed = registry.get("propose_patch").handler(path="source.py", find="value = 1", replace="value = 2")
    registry.get("apply_patch").handler(patch_id=proposed.data["patch_id"])
    assert path.read_bytes() == original.replace(b"value = 1", b"value = 2")
    assert journal.revert()["skipped"] == []
    assert path.read_bytes() == original


def test_newline_only_edit_refuses_stale_patch(tmp_path: Path) -> None:
    path = tmp_path / "source.py"
    path.write_bytes(b"value = 1\n")
    repo = RepoConfig(tmp_path, "fidelity", "build", "runs", {}, "default", Policy(allow_patch=True))
    registry, _, _ = build_registry(repo)
    proposed = registry.get("propose_patch").handler(path="source.py", find="value = 1", replace="value = 2")
    path.write_bytes(b"value = 1\r\n")
    with pytest.raises(ToolError, match="changed since"):
        registry.get("apply_patch").handler(patch_id=proposed.data["patch_id"])
    assert path.read_bytes() == b"value = 1\r\n"


@pytest.mark.parametrize("current", [b"value = 2\r\n", b"\xff"])
def test_rollback_preserves_subsequent_user_bytes(tmp_path: Path, current: bytes) -> None:
    path = tmp_path / "source.py"
    journal = MutationJournal()
    journal.record_write(path, "value = 1\n", "value = 2\n", "apply_patch")
    path.write_bytes(current)
    result = journal.revert()
    assert result["reverted"] == []
    assert len(result["skipped"]) == 1
    assert path.read_bytes() == current


@pytest.mark.parametrize("original, expected", [
    (b"head\r\n  a\r\n  b\r\ntail\r\n", b"head\r\n  a\r\n  c\r\ntail\r\n"),
    (b"head\n  a\r\n  b\ntail", b"head\n  a\r\n  c\ntail"),
    (b"head\r\n  a\r\n  b", b"head\r\n  a\r\n  c"),
])
def test_tolerant_multiline_patch_preserves_source_endings(tmp_path: Path, original: bytes, expected: bytes) -> None:
    path = tmp_path / "source.py"
    path.write_bytes(original)
    repo = RepoConfig(tmp_path, "fidelity", "build", "runs", {}, "default", Policy(allow_patch=True))
    registry, _, _ = build_registry(repo)
    proposed = registry.get("propose_patch").handler(path="source.py", find="a\nb", replace="a\nc")
    registry.get("apply_patch").handler(patch_id=proposed.data["patch_id"])
    assert path.read_bytes() == expected


@pytest.mark.parametrize("content", ["a\nb\n", "a\r\nb\r\n", "a\r\nb\nc"])
def test_new_file_preserves_explicit_bytes_and_owned_rollback(tmp_path: Path, content: str) -> None:
    journal = MutationJournal()
    repo = RepoConfig(tmp_path, "fidelity", "build", "runs", {}, "default", Policy(allow_patch=True))
    registry, _, _ = build_registry(repo, journal=journal)
    proposed = registry.get("propose_file").handler(path="source.py", content=content)
    registry.get("apply_patch").handler(patch_id=proposed.data["patch_id"])
    assert (tmp_path / "source.py").read_bytes() == content.encode("utf-8")
    assert journal.revert()["skipped"] == []
    assert not (tmp_path / "source.py").exists()
