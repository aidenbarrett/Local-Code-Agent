"""Patch ids survive a tool-call parser that coerces number-shaped parameter values."""
from __future__ import annotations

import json
import uuid

import pytest

from local_agent.config import load_repo_config
from local_agent.tools import build_registry
from local_agent.tools import patch as patch_tools

RING = "src/ring_buffer.cpp"


def _coerced_by_a_number_parser(value: str) -> str:
    """What a parser that tries JSON numbers first, then stringifies, hands back."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return str(parsed) if isinstance(parsed, (int, float)) else value


def _fixed_uuid(monkeypatch, hex_prefix: str) -> None:
    monkeypatch.setattr(patch_tools.uuid, "uuid4", lambda: uuid.UUID(hex_prefix + "0" * 24))


# 43477e22 is the id the GPU 30B run could not apply (it arrived as '4.3477e22').
# Any digits-e-digits id is a float literal in the same way.
NUMBER_SHAPED = ("43477e22", "123e4567")


@pytest.mark.parametrize("hex_prefix", NUMBER_SHAPED)
def test_a_bare_hex_id_of_this_shape_is_mangled_and_the_patch_id_is_not(monkeypatch, hex_prefix):
    assert _coerced_by_a_number_parser(hex_prefix) != hex_prefix
    _fixed_uuid(monkeypatch, hex_prefix)
    patch_id = patch_tools.new_patch_id()
    assert _coerced_by_a_number_parser(patch_id) == patch_id


@pytest.mark.parametrize("hex_prefix", NUMBER_SHAPED)
def test_number_shaped_ids_round_trip_through_propose_and_apply(sandbox, monkeypatch, hex_prefix):
    _fixed_uuid(monkeypatch, hex_prefix)
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    proposed = registry.get("propose_patch").handler(
        path=RING, find="    ++count_;\n", replace="    ++count_;  // one more\n")
    patch_id = _coerced_by_a_number_parser(proposed.data["patch_id"])
    assert registry.get("apply_patch").handler(patch_id=patch_id).ok
    assert "// one more" in (sandbox.root / RING).read_text(encoding="utf-8")
