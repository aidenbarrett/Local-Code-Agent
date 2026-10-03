from __future__ import annotations

from pathlib import Path

import pytest

from local_agent.tools import files
from local_agent.tools.files import _missing_file_message
from local_agent.tools.tool_primitives import NotFoundError


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


@pytest.mark.parametrize("requested", ["sandbox/ring_buffer.hpp", "sandbox\\ring_buffer.hpp"])
def test_unique_include_relative_header_is_read_without_a_guessing_loop(loaded, requested):
    _, _, registry, _, _ = loaded
    result = registry.get("read_file").handler(path=requested)
    assert Path(result.data["path"]) == Path("include/sandbox/ring_buffer.hpp")
    assert "class RingBuffer" in result.data["content"]
    # The summary names the file actually read, so a later patch targets the real path.
    assert result.summary.startswith(str(Path("include/sandbox/ring_buffer.hpp")))
    assert f"(resolved from {requested!r})" in result.summary


def test_a_direct_read_summary_is_unchanged(loaded):
    _, _, registry, _, _ = loaded
    result = registry.get("read_file").handler(path="include/sandbox/ring_buffer.hpp")
    assert result.summary.startswith("include/sandbox/ring_buffer.hpp lines 1-")
    assert "resolved from" not in result.summary


def test_qualified_definition_excludes_duplicate_short_names(loaded):
    sandbox, _, registry, _, _ = loaded
    duplicate = sandbox.root / "src" / "other.cpp"
    duplicate.write_text(
        "bool Other::full() const { return true; }\nbool full() { return false; }\n",
        encoding="utf-8",
    )

    qualified = registry.get("find_definition").handler(symbol="RingBuffer::full")
    fully_qualified = registry.get("find_definition").handler(symbol="sandbox::RingBuffer::full")
    short = registry.get("find_definition").handler(symbol="full")

    # Search order differs between ripgrep and the Python fallback; the fixture also
    # carries scenario copies of the source. Membership, not position, is the fact.
    sites = {(Path(m["file"]).as_posix(), m["line"]) for m in qualified.data["matches"]}
    assert ("src/ring_buffer.cpp", 29) in sites
    assert not any(Path(m["file"]).as_posix() == "src/other.cpp" for m in qualified.data["matches"])
    assert all("RingBuffer::full" in m["text"] for m in qualified.data["matches"])
    assert {
        (m["file"], m["line"], m["text"]) for m in fully_qualified.data["matches"]
    } == {
        (m["file"], m["line"], m["text"]) for m in qualified.data["matches"]
    }
    assert {m["text"] for m in short.data["matches"]} >= {
        "bool RingBuffer::full() const { return count_ == slots_.size(); }",
        "bool Other::full() const { return true; }",
        "bool full() { return false; }",
    }


def test_ambiguous_include_relative_header_is_not_chosen(loaded):
    sandbox, _, registry, _, _ = loaded
    duplicate = sandbox.root / "vendor" / "include" / "sandbox" / "ring_buffer.hpp"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("class WrongRingBuffer {};\n", encoding="utf-8")

    with pytest.raises(NotFoundError, match="existing paths to try") as refused:
        registry.get("read_file").handler(path="sandbox/ring_buffer.hpp")

    assert "include/sandbox/ring_buffer.hpp" in str(refused.value)
    assert "vendor/include/sandbox/ring_buffer.hpp" in str(refused.value)


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


@pytest.mark.parametrize("requested", ["missing/ring.cpp", "src/ring.cpp"])
def test_shallower_hints_displace_earlier_deep_matches(tmp_path, requested):
    names = [f"a/{i}/scenarios/src/ring.cpp" for i in range(8)]
    names += ["src/ring.cpp", "z/src/ring.cpp", "b/src/ring.cpp", "c/src/ring.cpp", "d/src/ring.cpp"]
    for name in reversed(names):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")
    hints = _missing_file_message(tmp_path, requested).split("existing paths to try: ")[1]
    assert hints.split(", ") == ["src/ring.cpp", "b/src/ring.cpp", "c/src/ring.cpp",
                                "d/src/ring.cpp", "z/src/ring.cpp"]


def test_deeper_suffix_still_beats_shallow_basename(tmp_path):
    for name in ["ring.hpp", "deep/include/sandbox/ring.hpp"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")
    hints = _missing_file_message(tmp_path, "sandbox/ring.hpp").split("existing paths to try: ")[1]
    assert hints == "deep/include/sandbox/ring.hpp"


def test_hint_search_is_bounded_and_says_where_it_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_MAX_HINT_SEARCH_FILES", 3)
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "wanted.hpp"]:
        (tmp_path / name).write_text("source")

    message = _missing_file_message(tmp_path, "wanted.hpp")

    assert "existing paths" not in message
    assert "hint search stopped after 3 files" in message


def test_match_before_hint_cap_is_reported_with_honest_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_MAX_HINT_SEARCH_FILES", 3)
    for name in ["a/ring.hpp", "b/item.txt", "c/item.txt", "d/item.txt"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")

    message = _missing_file_message(tmp_path, "missing/ring.hpp")

    assert "existing paths to try: a/ring.hpp" in message
    assert "hint search stopped after 3 files" in message


def test_excluded_directories_do_not_consume_hint_search_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_MAX_HINT_SEARCH_FILES", 1)
    hidden = tmp_path / "build" / "ring.hpp"
    hidden.parent.mkdir()
    hidden.write_text("excluded")
    visible = tmp_path / "ring.hpp"
    visible.write_text("source")

    message = _missing_file_message(tmp_path, "missing/ring.hpp")

    assert "existing paths to try: ring.hpp" in message
    assert "build/" not in message
    assert "search stopped" not in message
