"""Fast regressions for cross-platform build evidence representation."""

from __future__ import annotations

import json
from pathlib import Path

from local_agent.tools.testing import BuildRecord, _stale_sources, build_record


def test_stale_source_paths_are_canonical_repository_paths(tmp_path: Path):
    source = tmp_path / "src" / "nested" / "thing.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int x;\n", encoding="utf-8")

    record = BuildRecord(profile="debug", at_ns=source.stat().st_mtime_ns - 1)
    assert _stale_sources(tmp_path, "build", record) == ["src/nested/thing.cpp"]


def test_build_record_preserves_nanosecond_timestamp(tmp_path: Path):
    build = tmp_path / "build"
    build.mkdir()
    stamp = build / ".local-agent-build-ok"
    stamp.write_text(
        json.dumps({"profile": "debug", "at_ns": 1_234_567_890_123_456_789}),
        encoding="utf-8",
    )

    record = build_record(tmp_path, "build")
    assert record is not None
    assert record.profile == "debug"
    assert record.at_ns == 1_234_567_890_123_456_789


def test_previous_float_second_stamp_is_still_readable(tmp_path: Path):
    build = tmp_path / "build"
    build.mkdir()
    stamp = build / ".local-agent-build-ok"
    stamp.write_text(json.dumps({"profile": "debug", "at": 1234.5}), encoding="utf-8")

    record = build_record(tmp_path, "build")
    assert record is not None
    assert record.profile == "debug"
    assert record.at_ns == 1_234_500_000_000
