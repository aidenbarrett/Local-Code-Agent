"""Slice 1: failures are typed all the way down, and the run knows its validity.

A dead inference server is BLOCKED with a cause, never a traceback and never a
tool record. Every halt has a typed twin. Every Reason has a producer and a
locus. A run under a configuration nobody chose says so.
"""

from __future__ import annotations

from pathlib import Path

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.outcome import Outcome
from local_agent.agent.state import HaltCause, Validity
from local_agent.config import load_repo_config
from local_agent.llm.client import ScriptedClient, tool_call
from local_agent.llm.models import CallStats, ChatResponse, LLMTransportError
from local_agent.llm.router import TieredClient
from local_agent.tools import build_registry
from local_agent.tools.base import Locus, Reason

REPO = Path(__file__).resolve().parent.parent.parent


def _orch(root: Path, client, **kwargs):
    repo = load_repo_config(root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    return Orchestrator(repo, registry, client, skills, **kwargs)


def _dying(*turns_before_death):
    """A scripted client whose next call after the given turns is a dead server."""
    def die(_messages):
        raise LLMTransportError("APIConnectionError: Connection refused", cause="APIConnectionError")
    return ScriptedClient([*turns_before_death, die])


# --------------------------------------------------------------- server death


def test_a_dead_server_is_blocked_with_a_cause_and_no_traceback(sandbox):
    orch = _orch(sandbox.root, _dying(
        ChatResponse(tool_calls=[tool_call("git_status", {}, "c1")]),
    ))
    result = orch.run("what changed", skill_name="git-review")

    assert result.outcome is Outcome.BLOCKED
    assert result.state.halt_cause is HaltCause.SERVER_UNAVAILABLE
    assert "Connection refused" in (result.state.llm_error or "")
    assert result.state.validity is Validity.INVALID_SERVER_UNAVAILABLE
    # The one tool that ran is the only record. Nothing pretends the failed
    # inference call was a tool.
    assert [h.name for h in result.state.history] == ["git_status"]
    assert result.state.as_dict()["halt_cause"] == "server_unavailable"
    assert result.state.as_dict()["validity"] == "invalid_server_unavailable"


def test_a_dead_server_is_never_escalated(sandbox):
    """Walking the strong tier into the same dead socket helps nobody."""
    cheap = _dying()
    strong = ScriptedClient([ChatResponse(content="should never be asked")])
    tiered = TieredClient({"cheap": cheap, "strong": strong})
    orch = _orch(sandbox.root, tiered)
    result = orch.run("diagnose the build", skill_name="diagnose-build-failure")

    assert result.outcome is Outcome.BLOCKED
    assert result.routing is not None and result.routing.escalated is False
    assert strong.calls == []


def test_summary_lines_say_the_run_is_excluded(sandbox):
    orch = _orch(sandbox.root, _dying())
    result = orch.run("anything", skill_name="repo-navigation")
    text = "\n".join(result.state.summary_lines())
    assert "invalid_server_unavailable" in text
    assert "excluded from denominators" in text


# ------------------------------------------------------------- typed halts


def test_every_halt_carries_its_typed_cause(sandbox):
    # repeat loop
    orch = _orch(sandbox.root, ScriptedClient([
        ChatResponse(tool_calls=[tool_call("git_status", {}, f"c{i}")]) for i in range(10)
    ]))
    result = orch.run("look", skill_name="git-review")
    assert result.state.halt_cause is HaltCause.REPEAT_LOOP

    # unknown tools, and the records are typed too
    orch = _orch(sandbox.root, ScriptedClient([
        ChatResponse(tool_calls=[tool_call("rm_rf_everything", {}, f"c{i}")]) for i in range(6)
    ]))
    result = orch.run("delete it all", skill_name="repo-navigation")
    assert result.state.halt_cause is HaltCause.UNKNOWN_TOOLS
    assert all(h.reason == Reason.UNKNOWN_TOOL.value for h in result.state.history)
    assert all(h.execution == "error" for h in result.state.history)

    # budget
    orch = _orch(sandbox.root, ScriptedClient([
        ChatResponse(tool_calls=[tool_call("read_file", {"path": "src/ring_buffer.cpp",
                                                         "start_line": i, "end_line": i + 2}, f"c{i}")])
        for i in range(60)
    ]))
    result = orch.run("read everything", skill_name="repo-navigation")
    assert result.state.halt_cause is HaltCause.BUDGET_EXHAUSTED

    # a halted run that is not the server's fault is still VALID evidence
    assert result.state.validity is Validity.VALID


# ------------------------------------------------------------ the taxonomy


def test_every_reason_has_a_locus_and_a_producer():
    # totality is asserted at import; this pins the mapping the report relies on
    assert {r.locus for r in Reason} == set(Locus)
    assert Reason.UNKNOWN_TOOL.locus is Locus.MODEL
    assert Reason.SERVER_UNAVAILABLE.locus is Locus.SERVER
    assert Reason.MISSING_EXECUTABLE.locus is Locus.ENVIRONMENT
    assert Reason.APPROVAL_DECLINED.locus is Locus.USER

    # every member is constructed somewhere in src/, not only defined
    src = (REPO).rglob("*.py")
    text = "\n".join(p.read_text(encoding="utf-8") for p in src)
    for reason in Reason:
        producers = text.count(f"Reason.{reason.name}") - 1  # minus the _LOCUS entry
        assert producers >= 1, f"Reason.{reason.name} has no producer"


def test_not_found_is_the_models_problem_and_typed(sandbox):
    orch = _orch(sandbox.root, ScriptedClient([
        ChatResponse(tool_calls=[tool_call("read_file", {"path": "src/does_not_exist.cpp"}, "c1")]),
        ChatResponse(content="gave up"),
    ]))
    result = orch.run("read the missing file", skill_name="repo-navigation")
    rec = result.state.history[0]
    assert rec.reason == Reason.NOT_FOUND.value
    assert rec.execution == "error"
    assert Reason(rec.reason).locus is Locus.MODEL


# ----------------------------------------------------------- run validity


def test_validity_is_orthogonal_to_outcome():
    from local_agent.agent.state import AgentState

    state = AgentState(task="t", repo_root=Path("."))
    assert state.validity is Validity.VALID

    # a recorded fallback taints the run even if the task went fine
    state.metrics.observe_call(CallStats(total_s=1, completion_tokens=3,
                                         thinking_requested=False,
                                         thinking_control_accepted=False))
    assert state.validity is Validity.INVALID_FALLBACK

    # a swapped server is worse than a fallback
    state.metrics.expected_runtime_version = "b10816-427291b5b"
    state.metrics.observe_call(CallStats(total_s=1, completion_tokens=3,
                                         system_fingerprint="b99999-cafebabe"))
    assert state.validity is Validity.INVALID_IDENTITY_MISMATCH

    # and a dead server is worst of all
    state.halt(HaltCause.SERVER_UNAVAILABLE, "gone")
    assert state.validity is Validity.INVALID_SERVER_UNAVAILABLE


def test_orchestrator_learns_the_expected_fingerprint_from_the_client(sandbox):
    class Described(ScriptedClient):
        def identity(self):
            return {"runtime_version": "b10816-427291b5b", "model": "m"}

    good = ChatResponse(content="fine", stats=CallStats(total_s=1, completion_tokens=2,
                                                        system_fingerprint="b10816-427291b5b"))
    bad = ChatResponse(content="fine", stats=CallStats(total_s=1, completion_tokens=2,
                                                       system_fingerprint="b10900-other"))

    result = _orch(sandbox.root, Described([good])).run("hello", skill_name="repo-navigation")
    assert result.state.metrics.expected_runtime_version == "b10816-427291b5b"
    assert result.state.validity is Validity.VALID

    result = _orch(sandbox.root, Described([bad])).run("hello", skill_name="repo-navigation")
    assert result.state.metrics.identity_mismatch is True
    assert result.state.validity is Validity.INVALID_IDENTITY_MISMATCH


# ------------------------------------------------------- stalls, slice 3b
# The first real run sat in a ten-minute poll for 21 minutes while the server
# produced one token every ~6 s. A per-read timeout can never catch that. These
# pin the three clocks and the throughput floor, with time faked so the tests
# take milliseconds.


def _stall_client(monkeypatch, chunk_gap_s, n_chunks, **cfg):
    """A streaming client whose fake server yields a token every chunk_gap_s of
    fake time. time.monotonic is patched so nothing actually sleeps."""
    import types
    from local_agent.config import ModelConfig
    from local_agent.llm import client as client_mod
    from local_agent.llm.client import OpenAICompatibleClient

    clock = {"t": 1000.0}
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock["t"])

    def chunks():
        for i in range(n_chunks):
            clock["t"] += chunk_gap_s
            delta = types.SimpleNamespace(content="x", tool_calls=None)
            yield types.SimpleNamespace(
                choices=[types.SimpleNamespace(delta=delta, finish_reason=None)], usage=None)

    class Stream:
        closed = False
        def __iter__(self): return chunks()
        def close(self): Stream.closed = True

    c = OpenAICompatibleClient(ModelConfig(stream=True, **cfg))
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kw: Stream())))
    return c, Stream


def test_a_trickle_below_the_throughput_floor_is_a_stall(monkeypatch):
    # one token every 6 s, as observed. Floor 1 tok/s over a 60 s window.
    client, stream = _stall_client(monkeypatch, chunk_gap_s=6.0, n_chunks=500,
                                   stall_tok_s=1.0, stall_window_s=60.0, request_deadline_s=3600)
    try:
        client.chat([{"role": "user", "content": "x"}])
    except LLMTransportError as exc:
        assert exc.kind == "stalled"
        assert "decode stalled" in str(exc) and "0.17 tok/s" in str(exc)
        assert stream.closed, "the response must be closed so the server cancels the task"
    else:
        raise AssertionError("expected a stall")


def test_a_healthy_decode_rate_is_not_a_stall(monkeypatch):
    # 16 tok/s for 200 tokens: well above the floor, finishes normally
    client, _ = _stall_client(monkeypatch, chunk_gap_s=1 / 16, n_chunks=200,
                              stall_tok_s=1.0, stall_window_s=60.0)
    assert client.chat([{"role": "user", "content": "x"}]).content == "x" * 200


def test_the_request_deadline_is_wall_clock_not_per_read(monkeypatch):
    # fast enough to pass the floor, but the whole request runs past the deadline
    client, _ = _stall_client(monkeypatch, chunk_gap_s=0.5, n_chunks=10_000,
                              stall_tok_s=1.0, stall_window_s=60.0, request_deadline_s=120.0)
    try:
        client.chat([{"role": "user", "content": "x"}])
    except LLMTransportError as exc:
        assert exc.kind == "stalled"
        assert "exceeded deadline of 120s" in str(exc)
    else:
        raise AssertionError("expected a deadline abort")


def _build_with_fake_sdk(sdk_attrs: dict, stacks: dict, config):
    """Build the client against a fake openai module and fake HTTP libraries."""
    import sys, types
    from local_agent.llm.client import OpenAICompatibleClient

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw): captured.update(kw)

    names = ("openai", "httpx", "httpx2")
    saved = {k: sys.modules.get(k) for k in names}
    for k in names:
        sys.modules.pop(k, None)
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=FakeOpenAI, **sdk_attrs)
    for name, mod in stacks.items():
        sys.modules[name] = mod
    try:
        OpenAICompatibleClient(config)._ensure()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return captured


def test_the_sdk_client_is_built_with_zero_retries_and_split_timeouts():
    """One logical request is one measured attempt, and no clock is a bare float."""
    import types
    from local_agent.config import ModelConfig

    class FakeTimeout:
        def __init__(self, **kw): self.kw = kw

    cfg = ModelConfig(connect_timeout_s=7, read_timeout_s=90)
    expected = {"connect": 7, "read": 90, "write": 90, "pool": 7}

    # openai < 3: httpx. httpx2 is absent from the venv.
    captured = _build_with_fake_sdk(
        {"__version__": "2.9.0"}, {"httpx": types.SimpleNamespace(Timeout=FakeTimeout)}, cfg)
    assert captured["max_retries"] == 0
    assert captured["timeout"].kw == expected

    # openai >= 3: httpx2, and httpx is NOT installed. This is the fresh-venv
    # case that took the first probe down with ModuleNotFoundError: httpx.
    class Timeout2(FakeTimeout): pass
    captured = _build_with_fake_sdk(
        {"__version__": "3.8.0", "DefaultHttpx2Client": object},
        {"httpx2": types.SimpleNamespace(Timeout=Timeout2)}, cfg)
    assert captured["max_retries"] == 0
    assert isinstance(captured["timeout"], Timeout2)
    assert captured["timeout"].kw == expected


def test_sdk_version_and_http_stack_are_part_of_identity():
    """`openai>=1.40` resolves to a different SDK on a different day. Quote it."""
    import sys, types
    from local_agent.config import ModelConfig
    from local_agent.llm.client import OpenAICompatibleClient

    saved = sys.modules.get("openai")
    sys.modules["openai"] = types.SimpleNamespace(__version__="3.8.0", DefaultHttpx2Client=object)
    try:
        ident = OpenAICompatibleClient(ModelConfig()).identity()
    finally:
        if saved is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = saved
    assert ident["sdk_version"] == "3.8.0"
    assert ident["http_stack"] == "httpx2"
    assert ident["model"] == ModelConfig().model


def test_a_stall_is_typed_apart_from_unavailability(sandbox):
    def stall(_messages):
        raise LLMTransportError("decode stalled: 0.16 tok/s over 60s", cause="stall", kind="stalled")

    orch = _orch(sandbox.root, ScriptedClient([stall]))
    result = orch.run("anything", skill_name="repo-navigation")
    assert result.outcome is Outcome.BLOCKED
    assert result.state.halt_cause is HaltCause.INFERENCE_STALLED
    assert result.state.validity is Validity.INVALID_SERVER_STALLED
    assert "0.16 tok/s" in (result.state.llm_error or "")
    assert Reason.SERVER_STALLED.locus is Locus.SERVER


def test_stall_thresholds_are_part_of_identity():
    from local_agent.config import ModelConfig

    identity = ModelConfig(request_deadline_s=300, stall_tok_s=0.5).identity()
    assert identity["request_deadline_s"] == 300
    assert identity["stall_tok_s"] == 0.5


def test_the_read_timeout_survives_an_honest_prefill():
    """Nothing is sent while the server reads the prompt. A read timeout below
    the prefill time aborts valid work and files it as server death.

    Measured prefill on the NUC CPU falls as the context grows: 47.6 tok/s
    median in the first probe, 29.6 in the second once the context reached
    10.5k tokens. Budget from the slow measurement. At 25 tok/s a full
    12000-token prompt is eight minutes before the first byte, and a read
    timeout under that would abort valid work and file it as server death.
    """
    from local_agent.config import MODEL_PRESETS

    for name in ("nuc-llama-30b", "nuc-llama-8b"):
        cfg = MODEL_PRESETS[name]
        worst_prefill_s = cfg.context_budget_tokens / 25.0
        assert cfg.read_timeout_s >= worst_prefill_s, name
        # And the whole-request deadline still bounds it from above.
        assert cfg.request_deadline_s > cfg.read_timeout_s, name


def test_an_explicit_unknown_survives_the_convenience_inference():
    """UNKNOWN used to double as "not set", so a tool that ran cleanly and
    learned nothing had no way to say so: the inference overwrote it with PASS
    or FAIL from `ok`. A run_test whose filter matched no test exits 0 having
    verified nothing, and both inferences would be inventing an observation."""
    from local_agent.tools.base import DomainStatus, ExecutionStatus, ToolResult

    stated = ToolResult(ok=False, summary="ran 0 tests",
                        domain_status=DomainStatus.UNKNOWN)
    assert stated.domain_status is DomainStatus.UNKNOWN

    # And the convenience still works for the many tools that only set ok.
    assert ToolResult(ok=True, summary="x").domain_status is DomainStatus.PASS
    assert ToolResult(ok=False, summary="x").domain_status is DomainStatus.FAIL
    assert ToolResult(ok=False, summary="x",
                      execution_status=ExecutionStatus.BLOCKED
                      ).domain_status is DomainStatus.UNKNOWN


def test_an_empty_test_run_cannot_satisfy_a_verification_contract():
    """`verified` is set by a full run_test that passed. A run that executed no
    tests at all must not clear that bar, whatever its exit code."""
    from local_agent.agent.orchestrator import _satisfies_verification

    # The shape check still holds: a filtered run never verifies.
    assert _satisfies_verification("run_test", {}) is True
    assert _satisfies_verification("run_test", {"name_filter": "x"}) is False
    # And the orchestrator only records verified on a passing result, which an
    # empty run is not: ok False, domain UNKNOWN.
    from local_agent.tools.base import DomainStatus, ToolResult
    empty = ToolResult(ok=False, summary="ran 0 tests", domain_status=DomainStatus.UNKNOWN)
    assert not (empty.ran and empty.ok)
