from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import sys

import pytest


INTERNAL = Path(__file__).resolve().parents[2]


def _context():
    path = INTERNAL / "scripts" / "chat_context.py"
    spec = spec_from_file_location("chat_context_test_target", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _session(ctx, budget=1000):
    return ctx.new_session("p", "m", "NPU", budget_chars=budget)


def test_round_trip_preserves_raw_history(tmp_path):
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", r"open C:\private\work\file.cpp", 0)
    ctx.append_turn(session, "assistant", "/home/aiden/private/result", 0)
    ctx.save_session(tmp_path, session)

    loaded = ctx.load_session(tmp_path, session.conversation_id)
    assert [turn.content for turn in loaded.turns] == [
        r"open C:\private\work\file.cpp",
        "/home/aiden/private/result",
    ]


def test_unknown_schema_refuses_with_found_and_supported_versions(tmp_path):
    ctx = _context()
    session = _session(ctx)
    path = ctx.save_session(tmp_path, session)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ctx.ContextRefusal, match="schema 99; supported schema is 1"):
        ctx.load_session(tmp_path, session.conversation_id)


def test_runtime_change_appends_segment_and_turns_keep_provenance():
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "first", 0)
    ctx.append_turn(session, "assistant", "reply", 0)
    second = ctx.ensure_runtime(session, "p2", "m2", "GPU")
    ctx.append_turn(session, "user", "second", second)

    assert second == 1
    assert len(session.runtime) == 2
    assert session.runtime[1].from_turn == 2
    assert [turn.runtime for turn in session.turns] == [0, 0, 1]


def test_compose_is_deterministic_and_drops_oldest_whole_turns():
    ctx = _context()
    session = _session(ctx, budget=24)
    ctx.append_turn(session, "user", "111111", 0)
    ctx.append_turn(session, "assistant", "222222", 0)
    contract = {"role": "system", "content": "contract"}

    first = ctx.compose(contract=contract, persona=None, session=session, current_turn="333333")
    second = ctx.compose(contract=contract, persona=None, session=session, current_turn="333333")

    assert first == second
    assert first.dropped_turns == 1
    assert first.messages == [contract, {"role": "assistant", "content": "222222"}, {"role": "user", "content": "333333"}]


def test_compose_refuses_instead_of_truncating_fixed_contract():
    ctx = _context()
    session = _session(ctx, budget=5)
    with pytest.raises(ctx.ContextRefusal, match="exceed character budget"):
        ctx.compose(
            contract={"role": "system", "content": "contract"},
            persona=None,
            session=session,
            current_turn="x",
        )


def test_disk_cap_refuses_without_dropping_history(tmp_path):
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "x" * 100, 0)
    with pytest.raises(ctx.ContextRefusal, match="exceeds disk cap"):
        ctx.save_session(tmp_path, session, disk_cap_bytes=50)
    assert not ctx.session_path(tmp_path, session.conversation_id).exists()
    assert len(session.turns) == 1


def test_cross_conversation_isolation(tmp_path):
    ctx = _context()
    one = _session(ctx)
    two = _session(ctx)
    ctx.append_turn(one, "user", "one", 0)
    ctx.append_turn(two, "user", "two", 0)
    ctx.save_session(tmp_path, one)
    ctx.save_session(tmp_path, two)

    assert ctx.load_session(tmp_path, one.conversation_id).turns[0].content == "one"
    assert ctx.load_session(tmp_path, two.conversation_id).turns[0].content == "two"


def test_atomic_replace_failure_keeps_previous_version(monkeypatch, tmp_path):
    ctx = _context()
    session = _session(ctx)
    path = ctx.save_session(tmp_path, session)
    before = path.read_bytes()
    ctx.append_turn(session, "user", "new", 0)

    monkeypatch.setattr(ctx.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError, match="crash"):
        ctx.save_session(tmp_path, session)
    assert path.read_bytes() == before
