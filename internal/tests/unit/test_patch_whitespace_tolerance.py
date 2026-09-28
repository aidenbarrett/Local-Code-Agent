"""propose_patch tolerates indentation/trailing-space slips, but only for one whole-line block."""
from __future__ import annotations

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry
from local_agent.tools.patch import _tolerant_replace
from local_agent.tools.tool_primitives import ToolError

RING = "src/ring_buffer.cpp"


def _propose(sandbox):
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    return registry.get("propose_patch").handler, registry.get("apply_patch").handler


def test_exact_matches_are_unchanged(sandbox):
    propose, _apply = _propose(sandbox)
    result = propose(path=RING, find="    ++count_;\n", replace="    ++count_;  // one more\n")
    assert result.data["match"] == "exact"
    assert "check the diff" not in result.summary


def test_wrong_indentation_is_matched_and_the_replacement_takes_the_files_indent(sandbox):
    propose, apply = _propose(sandbox)
    # The model dropped the 4-space indent and added trailing spaces.
    result = propose(path=RING, find="if (full()) {  \n    return false;\n}",
                     replace="if (full()) {\n    return false;  // rejected\n}")
    assert result.data["match"] == "whitespace_tolerant"
    assert "check the diff" in result.summary
    apply(patch_id=result.data["patch_id"])
    text = (sandbox.root / RING).read_text(encoding="utf-8")
    assert "    if (full()) {\n        return false;  // rejected\n    }\n" in text


def test_partial_lines_never_match_tolerantly(sandbox):
    propose, _apply = _propose(sandbox)
    with pytest.raises(ToolError, match="even ignoring indentation"):
        propose(path=RING, find="return count_  == 0;", replace="return true;")


def test_an_ambiguous_tolerant_match_is_refused(sandbox):
    propose, _apply = _propose(sandbox)
    # No exact match (the file has no tabs), and a lone closing brace is on many lines.
    with pytest.raises(ToolError, match="places once whitespace is ignored"):
        propose(path=RING, find="\t}", replace="}")


def test_indent_width_is_scaled_and_tabs_are_respected():
    src = "int f() {\n    if (x) {\n        return 1;\n    }\n}\n"
    updated, blocks = _tolerant_replace(src, "if (x) {\n  return 1;\n}", "if (x) {\n  return 2;\n  log();\n}")
    assert blocks == 1
    assert updated == "int f() {\n    if (x) {\n        return 2;\n        log();\n    }\n}\n"
    tabbed = "void g() {\n\tif (a) {\n\t\tb();\n\t}\n}\n"
    updated, _ = _tolerant_replace(tabbed, "if (a) {\n    b();\n}", "if (a) {\n    c();\n}")
    assert updated == "void g() {\n\tif (a) {\n\t\tc();\n\t}\n}\n"


def test_blank_find_never_matches():
    assert _tolerant_replace("a\n\nb\n", "\n   \n", "x") == (None, 0)


def test_read_file_line_numbers_echoed_into_find_are_removed(sandbox):
    # read_file shows `13:     ++count_;`; a small model copies that into find/replace.
    propose, apply = _propose(sandbox)
    shown = (sandbox.root / RING).read_text(encoding="utf-8").splitlines()
    line = next(i for i, text in enumerate(shown, start=1) if text.strip() == "++count_;")
    result = propose(path=RING, find=f"{line}:     ++count_;",
                     replace=f"{line}:     ++count_;  // one more")
    assert result.data["match"] == "line_numbers_stripped"
    assert "line numbers" in result.summary
    apply(patch_id=result.data["patch_id"])
    text = (sandbox.root / RING).read_text(encoding="utf-8")
    assert "    ++count_;  // one more\n" in text
    assert f"{line}:" not in text


def test_line_numbers_are_only_stripped_when_every_line_carries_them(sandbox):
    propose, _apply = _propose(sandbox)
    with pytest.raises(ToolError, match="does not appear"):
        propose(path=RING, find="12: nothing like this\nstill nothing", replace="x")


def test_the_prefix_rule_needs_every_nonblank_line_numbered():
    from local_agent.tools.patch import _without_read_file_numbers
    assert _without_read_file_numbers("1: a\n\n2:   b") == "a\n\n  b"
    assert _without_read_file_numbers("1: a\nplain") is None
    assert _without_read_file_numbers("") is None
