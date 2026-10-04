"""Full-build freshness covers conservative repository input identity (#400)."""

import os
from pathlib import Path

from local_agent.tools.testing_tools import BuildRecord, _source_hashes, _stale_sources


def _record(root: Path) -> BuildRecord:
    return BuildRecord("debug", 1, _source_hashes(root, "build"))


def test_non_suffix_input_changes_are_stale_even_with_equal_mtime(tmp_path: Path) -> None:
    resource = tmp_path / "weights.bin"
    resource.write_bytes(b"old model bytes")
    record = _record(tmp_path)
    before = resource.stat().st_mtime_ns

    resource.write_bytes(b"new model bytes")
    os.utime(resource, ns=(before, before))

    assert _stale_sources(tmp_path, "build", record) == ["weights.bin"]


def test_add_delete_and_rename_non_suffix_inputs_are_stale(tmp_path: Path) -> None:
    original = tmp_path / "linker.ldscript"
    original.write_text("SECTIONS {}")
    record = _record(tmp_path)
    original.rename(tmp_path / "renamed.resource")
    (tmp_path / "added.json").write_text("{}")

    assert _stale_sources(tmp_path, "build", record) == [
        "added.json", "linker.ldscript", "renamed.resource",
    ]


def test_external_symlink_is_always_unknown_build_input(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"first")
    (root / "input.bin").symlink_to(outside)
    record = _record(root)

    assert _stale_sources(root, "build", record) == ["input.bin"]


def test_internal_symlink_is_bound_by_link_and_target_identity(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"first")
    (tmp_path / "input.bin").symlink_to(target.name)
    record = _record(tmp_path)

    target.write_bytes(b"second")

    assert _stale_sources(tmp_path, "build", record) == ["data.bin"]
