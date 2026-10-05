from __future__ import annotations

import os

from local_agent.tools.testing_tools import (
    BuildRecord,
    _source_hashes,
    changed_build_inputs,
    finish_input_observation,
    start_test_input_observation,
)


def test_input_identity_detects_add_delete_and_equal_mtime_change(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    changed = root / "changed.bin"
    deleted = root / "deleted.input"
    changed.write_bytes(b"before")
    deleted.write_bytes(b"present")
    before = _source_hashes(root, "build")

    old_stat = changed.stat()
    changed.write_bytes(b"after!")
    os.utime(changed, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    deleted.unlink()
    (root / "added.resource").write_bytes(b"new")

    assert changed_build_inputs(before, _source_hashes(root, "build")) == [
        "added.resource",
        "changed.bin",
        "deleted.input",
    ]


def test_generated_build_outputs_do_not_invalidate_input_identity(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.cpp").write_text("int main() {}\n")
    before = _source_hashes(root, "build")

    output = root / "build" / "generated" / "result.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"compiler output")

    changed, stale = finish_input_observation(root, "build", before, [])
    assert changed == []
    assert stale == []


def test_test_start_uses_one_identity_for_record_and_temporal_comparison(
    tmp_path, monkeypatch
):
    """A second start read used to combine mutually incompatible observations."""
    from local_agent.tools import testing_tools

    root = tmp_path / "repo"
    root.mkdir()
    source = root / "input.cpp"
    source.write_bytes(b"unbuilt A")
    built = {"input.cpp": testing_tools.hashlib.sha256(b"built B").hexdigest()}
    record = BuildRecord("debug", 1, built)
    monkeypatch.setattr(testing_tools, "build_record", lambda *_: record)
    real_hashes = testing_tools._source_hashes

    def capture_then_move(*args):
        identity = real_hashes(*args)
        source.write_bytes(b"built B")
        return identity

    monkeypatch.setattr(testing_tools, "_source_hashes", capture_then_move)
    observed_record, before, stale = start_test_input_observation(root, "build")
    source.write_bytes(b"unbuilt A")
    changed, final_stale = finish_input_observation(root, "build", before, stale)

    assert observed_record is record
    assert stale == ["input.cpp"]
    assert changed == []
    assert final_stale == ["input.cpp"]
