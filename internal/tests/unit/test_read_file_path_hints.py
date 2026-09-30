from __future__ import annotations

import pytest

from local_agent.tools.files import _missing_file_message
from local_agent.tools.tool_primitives import NotFoundError, ToolError


def test_suffix_hint_is_preferred_over_basename(tmp_path):
    for name in ["include/sandbox/ring.hpp", "other/ring.hpp", "build/sandbox/ring.hpp", ".git/ring.hpp"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret")
    message = _missing_file_message(tmp_path, "sandbox/ring.hpp")
    assert "include/sandbox/ring.hpp" in message
    assert "other/ring.hpp" not in message
    assert "build/" not in message
    assert ".git/" not in message


def test_basename_fallback_is_bounded(tmp_path):
    for index in range(8):
        path = tmp_path / str(index) / "ring.hpp"
        path.parent.mkdir()
        path.write_text("secret")
    message = _missing_file_message(tmp_path, "missing/ring.hpp")
    assert message.split("existing paths to try: ")[1].split(", ") == [f"{i}/ring.hpp" for i in range(5)]


def test_no_match_has_no_invented_hint(tmp_path):
    assert _missing_file_message(tmp_path, "missing.hpp") == "'missing.hpp' is not a file"


def test_missing_read_remains_not_found(loaded):
    _, _, registry, _, _ = loaded
    with pytest.raises(NotFoundError, match="include/sandbox/ring_buffer.hpp"):
        registry.get("read_file").handler(path="sandbox/ring_buffer.hpp")


def test_qualified_definition_names_the_identifier_to_use(loaded):
    _, _, registry, _, _ = loaded
    with pytest.raises(ToolError, match="use 'full'"):
        registry.get("find_definition").handler(symbol="RingBuffer::full")


def test_suffix_matches_are_bounded(tmp_path):
    for index in range(8):
        path = tmp_path / str(index) / 'sandbox' / 'ring.hpp'
        path.parent.mkdir(parents=True)
        path.write_text('secret')
    hints = _missing_file_message(tmp_path, 'sandbox/ring.hpp').split('existing paths to try: ')[1]
    assert hints.split(', ') == [f'{i}/sandbox/ring.hpp' for i in range(5)]


def test_external_symlink_is_not_a_hint(tmp_path):
    outside = tmp_path.parent / 'external.hpp'
    outside.write_text('secret')
    try:
        (tmp_path / 'ring.hpp').symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable')
    assert 'existing paths' not in _missing_file_message(tmp_path, 'missing/ring.hpp')
