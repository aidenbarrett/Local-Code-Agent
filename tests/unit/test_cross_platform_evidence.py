"""Fast regressions for cross-platform build evidence representation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from local_agent.tools.testing import BuildRecord, _stale_sources, build_record


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_stale_source_paths_are_canonical_repository_paths(tmp_path: Path):
    source = tmp_path / "src" / "nested" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\n", encoding="utf-8")

    record = BuildRecord(
        profile="debug", at_ns=1,
        source_hashes={"src/nested/thing.cpp": _sha(b"int y;\n")},
    )
    assert _stale_sources(tmp_path, "build", record) == ["src/nested/thing.cpp"]


def test_staleness_is_content_based_even_when_mtime_is_identical(tmp_path: Path):
    source = tmp_path / "src" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int before;\n", encoding="utf-8")
    stat = source.stat()
    record = BuildRecord(
        profile="debug", at_ns=stat.st_mtime_ns,
        source_hashes={"src/thing.cpp": _sha(source.read_bytes())},
    )

    source.write_text("int after;\n", encoding="utf-8")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert source.stat().st_mtime_ns == stat.st_mtime_ns
    assert _stale_sources(tmp_path, "build", record) == ["src/thing.cpp"]


def test_build_record_preserves_content_snapshot(tmp_path: Path):
    build = tmp_path / "build"
    build.mkdir()
    stamp = build / ".local-agent-build-ok"
    stamp.write_text(
        json.dumps({
            "profile": "debug",
            "at_ns": 1_234_567_890_123_456_789,
            "source_hashes": {"src/x.cpp": "a" * 64},
        }),
        encoding="utf-8",
    )

    record = build_record(tmp_path, "build")
    assert record is not None
    assert record.profile == "debug"
    assert record.at_ns == 1_234_567_890_123_456_789
    assert record.source_hashes == {"src/x.cpp": "a" * 64}


def test_timestamp_only_legacy_stamp_is_unknown_provenance(tmp_path: Path):
    build = tmp_path / "build"
    build.mkdir()
    stamp = build / ".local-agent-build-ok"
    stamp.write_text(
        json.dumps({"profile": "debug", "at_ns": 1_234_567_890}),
        encoding="utf-8",
    )

    assert build_record(tmp_path, "build") is None


def test_deleted_build_input_is_stale(tmp_path: Path):
    source = tmp_path / "src" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\n", encoding="utf-8")
    record = BuildRecord(
        profile="debug", at_ns=1,
        source_hashes={"src/thing.cpp": _sha(source.read_bytes())},
    )
    source.unlink()
    assert _stale_sources(tmp_path, "build", record) == ["src/thing.cpp"]


def test_ancestor_named_build_does_not_disable_staleness(tmp_path: Path):
    root = tmp_path / "build" / "worktree"
    source = root / "src" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\n", encoding="utf-8")
    record = BuildRecord(
        profile="debug", at_ns=1,
        source_hashes={"src/thing.cpp": _sha(b"int y;\n")},
    )
    assert _stale_sources(root, "build-dir", record) == ["src/thing.cpp"]
