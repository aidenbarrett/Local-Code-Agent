from contextlib import contextmanager
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


def _session(ctx, budget=1000, persona_sha256=None):
    return ctx.new_session("p", "m", "NPU", budget_chars=budget, persona_sha256=persona_sha256)


def _create(ctx, tmp_path, session):
    ctx.create_session(tmp_path, session)
    return session


def _read(ctx, tmp_path, conversation_id):
    with ctx.conversation(tmp_path, conversation_id) as opened:
        return opened.session


def test_round_trip_preserves_raw_history_and_persona_provenance(tmp_path):
    ctx = _context()
    persona_sha = "a" * 64
    session = _session(ctx, persona_sha256=persona_sha)
    ctx.append_turn(session, "user", r"open C:\private\work\file.cpp", 0)
    ctx.append_turn(session, "assistant", "/home/aiden/private/result", 0)
    _create(ctx, tmp_path, session)

    loaded = _read(ctx, tmp_path, session.conversation_id)
    assert loaded.persona_sha256 == persona_sha
    assert [turn.content for turn in loaded.turns] == [
        r"open C:\private\work\file.cpp",
        "/home/aiden/private/result",
    ]


def test_invalid_persona_hash_refuses(tmp_path):
    ctx = _context()
    session = _session(ctx)
    path = ctx.create_session(tmp_path, session)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["persona_sha256"] = "not-a-sha"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ctx.ContextRefusal, match="invalid persona_sha256"):
        with ctx.conversation(tmp_path, session.conversation_id):
            pass


def test_unknown_schema_refuses_with_found_and_supported_versions(tmp_path):
    ctx = _context()
    session = _session(ctx)
    path = ctx.create_session(tmp_path, session)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ctx.ContextRefusal, match="schema 99; supported schema is 1"):
        with ctx.conversation(tmp_path, session.conversation_id):
            pass


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


def test_append_turn_refuses_stale_runtime_segment():
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "first", 0)
    ctx.ensure_runtime(session, "p2", "m2", "GPU")

    with pytest.raises(ctx.ContextRefusal, match="current runtime segment 1"):
        ctx.append_turn(session, "assistant", "wrong provenance", 0)


def test_load_refuses_unordered_runtime_segments(tmp_path):
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "first", 0)
    ctx.append_turn(session, "assistant", "reply", 0)
    second = ctx.ensure_runtime(session, "p2", "m2", "GPU")
    ctx.append_turn(session, "user", "second", second)
    path = ctx.create_session(tmp_path, session)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["runtime"][0]["from_turn"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ctx.ContextRefusal, match="first runtime segment must start at turn 0"):
        with ctx.conversation(tmp_path, session.conversation_id):
            pass


def test_conversation_loads_only_after_lock_acquisition(monkeypatch, tmp_path):
    ctx = _context()
    session = _create(ctx, tmp_path, _session(ctx))

    @contextmanager
    def interleaving_lock(runtime_root, conversation_id):
        # Simulate a writer that commits after this opener starts but before it
        # acquires ownership. Correct code loads only after this yield begins.
        latest = ctx._load_session(runtime_root, conversation_id)
        ctx.append_turn(latest, "user", "newer committed turn", 0)
        ctx._save_session(runtime_root, latest)
        yield

    monkeypatch.setattr(ctx, "_conversation_lock", interleaving_lock)

    with ctx.conversation(tmp_path, session.conversation_id) as opened:
        assert [turn.content for turn in opened.session.turns] == ["newer committed turn"]


def test_conversation_context_does_not_auto_save_abandoned_edit(tmp_path):
    ctx = _context()
    session = _create(ctx, tmp_path, _session(ctx))

    with ctx.conversation(tmp_path, session.conversation_id) as opened:
        ctx.append_turn(opened.session, "user", "not committed", 0)

    loaded = _read(ctx, tmp_path, session.conversation_id)
    assert loaded.turns == []


def test_create_session_refuses_replacing_existing_conversation(tmp_path):
    ctx = _context()
    session = _create(ctx, tmp_path, _session(ctx))

    with pytest.raises(ctx.ContextRefusal, match="already exists"):
        ctx.create_session(tmp_path, session)


def test_compose_is_deterministic_and_drops_oldest_complete_exchange():
    ctx = _context()
    session = _session(ctx, budget=24)
    ctx.append_turn(session, "user", "111111", 0)
    ctx.append_turn(session, "assistant", "222222", 0)
    contract = {"role": "system", "content": "contract"}

    first = ctx.compose(contract=contract, persona=None, session=session, current_turn="333333")
    second = ctx.compose(contract=contract, persona=None, session=session, current_turn="333333")

    assert first == second
    assert first.dropped_turns == 2
    assert first.messages == [contract, {"role": "user", "content": "333333"}]


def test_compose_retained_history_never_starts_with_assistant():
    ctx = _context()
    session = _session(ctx, budget=35)
    for role, content in [
        ("user", "111111"),
        ("assistant", "222222"),
        ("user", "333333"),
        ("assistant", "444444"),
    ]:
        ctx.append_turn(session, role, content, 0)
    contract = {"role": "system", "content": "contract"}

    composed = ctx.compose(contract=contract, persona=None, session=session, current_turn="555555")
    history = composed.messages[1:-1]
    assert not history or history[0]["role"] == "user"
    assert [item["role"] for item in history] in ([], ["user", "assistant"])


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


def test_projected_disk_cap_refuses_before_live_session_mutates(tmp_path):
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "saved", 0)
    ctx.create_session(tmp_path, session)
    before_turns = list(session.turns)
    before_disk = ctx.session_path(tmp_path, session.conversation_id).read_bytes()

    with pytest.raises(ctx.ContextRefusal, match="would exceed disk cap"):
        ctx.ensure_append_fits(session, [("assistant", "x" * 1000)], 0, disk_cap_bytes=900)

    assert session.turns == before_turns
    assert ctx.session_path(tmp_path, session.conversation_id).read_bytes() == before_disk


def test_disk_cap_refuses_without_dropping_history(tmp_path):
    ctx = _context()
    session = _session(ctx)
    ctx.append_turn(session, "user", "x" * 100, 0)
    with pytest.raises(ctx.ContextRefusal, match="exceeds disk cap"):
        ctx.create_session(tmp_path, session, disk_cap_bytes=50)
    assert not ctx.session_path(tmp_path, session.conversation_id).exists()
    assert len(session.turns) == 1


def test_cross_conversation_isolation(tmp_path):
    ctx = _context()
    one = _session(ctx)
    two = _session(ctx)
    ctx.append_turn(one, "user", "one", 0)
    ctx.append_turn(two, "user", "two", 0)
    ctx.create_session(tmp_path, one)
    ctx.create_session(tmp_path, two)

    assert _read(ctx, tmp_path, one.conversation_id).turns[0].content == "one"
    assert _read(ctx, tmp_path, two.conversation_id).turns[0].content == "two"


def test_atomic_replace_failure_keeps_previous_version(monkeypatch, tmp_path):
    ctx = _context()
    session = _session(ctx)
    path = ctx.create_session(tmp_path, session)
    before = path.read_bytes()

    with ctx.conversation(tmp_path, session.conversation_id) as opened:
        ctx.append_turn(opened.session, "user", "new", 0)
        monkeypatch.setattr(ctx.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError, match="crash"):
            opened.save()

    assert path.read_bytes() == before
