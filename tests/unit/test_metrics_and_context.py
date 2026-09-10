"""Tests for the measurement layer.

The whole point of this project is to produce numbers somebody will act on, so
the code that produces the numbers gets tested harder than the code that
produces the prose.
"""

from __future__ import annotations

import json
import types

from local_agent.agent.context import ContextManager, approximate_tokens, trim
from local_agent.llm.models import CallStats, ChatResponse, ToolCall


def _tool_message(index: int, payload_chars: int = 4000) -> dict:
    return {
        "role": "tool",
        "tool_call_id": f"c{index}",
        "name": "build_target",
        "content": json.dumps(
            {
                "ok": False,
                "summary": f"build FAILED run {index}",
                "artifacts": [f"runs/{index}/combined.log"],
                "data": {"blob": "x" * payload_chars},
            }
        ),
    }


# ------------------------------------------------------------------ context


def test_context_is_append_only_until_it_has_to_compact():
    ctx = ContextManager(budget_tokens=100_000)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(10):
        ctx.append({"role": "assistant", "content": None})
        ctx.append(_tool_message(i))

    snapshot = [dict(m) for m in ctx.for_request()]
    ctx.append(_tool_message(99))

    # Nothing already sent was rewritten, so a server prefix cache still matches.
    assert ctx.for_request()[: len(snapshot)] == snapshot
    assert ctx.compactions == []
    assert ctx.stable_prefix_len == len(ctx.messages)


def test_compaction_fires_once_and_is_recorded():
    ctx = ContextManager(budget_tokens=3_000, keep_recent_tool_results=2)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(12):
        ctx.append({"role": "assistant", "content": None})
        ctx.append(_tool_message(i))

    assert ctx.needs_compaction()
    event = ctx.compact()
    assert event is not None
    assert event.tokens_reclaimed > 0
    assert event.collapsed_results > 0
    assert len(ctx.compactions) == 1
    assert ctx.stable_prefix_len < len(ctx.messages)


def test_compaction_undershoots_the_budget_so_it_does_not_immediately_refire():
    ctx = ContextManager(budget_tokens=3_000, keep_recent_tool_results=2, hysteresis=0.55)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(12):
        ctx.append({"role": "assistant", "content": None})
        ctx.append(_tool_message(i))

    ctx.compact()
    after = ctx.tokens
    assert after < ctx.budget_tokens * 0.9, (after, ctx.budget_tokens)
    # One more turn of growth must not immediately need another compaction.
    ctx.append({"role": "assistant", "content": None})
    ctx.append(_tool_message(50, payload_chars=800))
    assert not ctx.needs_compaction()


def test_recent_results_survive_compaction_intact():
    ctx = ContextManager(budget_tokens=2_000, keep_recent_tool_results=2)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(10):
        ctx.append(_tool_message(i))
    ctx.compact()

    assert "collapsed" in ctx.messages[1]["content"]
    assert "x" * 100 in ctx.messages[-1]["content"]


def test_compacting_twice_does_not_double_count():
    ctx = ContextManager(budget_tokens=2_000, keep_recent_tool_results=2)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(10):
        ctx.append(_tool_message(i))
    ctx.compact()
    # Already collapsed messages are skipped, so a second call finds nothing.
    assert ctx.compact() is None


def test_legacy_trim_still_works():
    messages = [{"role": "system", "content": "sys"}] + [_tool_message(i) for i in range(8)]
    before = approximate_tokens(messages)
    after = trim(messages, budget_tokens=2_000)
    assert approximate_tokens(after) < before


# -------------------------------------------------------------- call stats


def test_call_stats_splits_prefill_and_decode():
    stats = CallStats(
        total_s=10.0,
        ttft_s=4.0,
        prompt_tokens=4000,
        completion_tokens=61,
        cached_tokens=0,
        streamed=True,
    )
    assert stats.decode_s == 6.0
    assert stats.prefill_tok_s == 1000.0
    assert stats.decode_tok_s == 10.0
    assert stats.cache_hit_ratio == 0.0


def test_cached_tokens_are_excluded_from_the_prefill_rate():
    stats = CallStats(
        total_s=3.0, ttft_s=1.0, prompt_tokens=4000, completion_tokens=21,
        cached_tokens=3000, streamed=True,
    )
    # Only the 1000 uncached tokens were actually prefilled.
    assert stats.prefill_tok_s == 1000.0
    assert stats.cache_hit_ratio == 0.75


def test_no_ttft_means_no_invented_split():
    stats = CallStats(total_s=9.0, ttft_s=None, prompt_tokens=100, completion_tokens=50)
    assert stats.decode_s is None
    assert stats.decode_tok_s is None
    assert stats.prefill_tok_s is None
    assert stats.as_dict()["ttft_s"] is None


def test_run_metrics_aggregate_and_attribute_wall_clock(sandbox):
    from local_agent.agent.state import RunMetrics

    metrics = RunMetrics()
    metrics.observe_call(
        CallStats(total_s=5.0, ttft_s=4.0, prompt_tokens=2000, completion_tokens=21)
    )
    metrics.observe_call(
        CallStats(total_s=3.0, ttft_s=1.0, prompt_tokens=2400, completion_tokens=11,
                  cached_tokens=2000)
    )
    metrics.tool_seconds = 2.0
    metrics.wall_seconds = 11.0

    out = metrics.as_dict()
    assert out["llm_calls"] == 2
    assert out["llm_seconds"] == 8.0
    assert out["overhead_seconds"] == 1.0
    assert out["median_ttft_s"] == 2.5
    assert out["cache_hit_ratio"] == round(2000 / 4400, 3)


# ---------------------------------------------------------------- streaming


class _FakeStream:
    """Mimics the chunk shapes an OpenAI-compatible server emits."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


def _chunk(content=None, tool_deltas=None, finish=None, usage=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_deltas)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish)
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return types.SimpleNamespace(
        index=index,
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=arguments),
    )


def _client_with(chunks):
    from local_agent.config import ModelConfig
    from local_agent.llm.client import OpenAICompatibleClient

    client = OpenAICompatibleClient(ModelConfig(stream=True))
    fake_completions = types.SimpleNamespace(
        create=lambda **kwargs: _FakeStream(chunks)
    )
    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=fake_completions)
    )
    return client


def test_streaming_accumulates_tool_call_argument_fragments():
    usage = types.SimpleNamespace(
        prompt_tokens=1234,
        completion_tokens=17,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=1000),
    )
    chunks = [
        _chunk(tool_deltas=[_tool_delta(0, "call_a", "read_file", '{"path": "sr')]),
        _chunk(tool_deltas=[_tool_delta(0, arguments='c/ring_buffer.cpp",')]),
        _chunk(tool_deltas=[_tool_delta(0, arguments=' "start_line": 1}')]),
        _chunk(finish="tool_calls"),
        _chunk(usage=usage),
    ]
    response = _client_with(chunks).chat([{"role": "user", "content": "go"}])

    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments == {"path": "src/ring_buffer.cpp", "start_line": 1}
    assert response.finish_reason == "tool_calls"
    assert response.stats.streamed is True
    assert response.stats.prompt_tokens == 1234
    assert response.stats.cached_tokens == 1000
    assert response.stats.ttft_s is not None


def test_streaming_handles_two_parallel_tool_calls():
    chunks = [
        _chunk(tool_deltas=[_tool_delta(0, "a", "git_status", "{}")]),
        _chunk(tool_deltas=[_tool_delta(1, "b", "git_diff", '{"staged"')]),
        _chunk(tool_deltas=[_tool_delta(1, arguments=": true}")]),
        _chunk(finish="tool_calls"),
    ]
    response = _client_with(chunks).chat([{"role": "user", "content": "go"}])
    assert [c.name for c in response.tool_calls] == ["git_status", "git_diff"]
    assert response.tool_calls[1].arguments == {"staged": True}


def test_streaming_plain_text_measures_ttft():
    chunks = [
        _chunk(content="The build "),
        _chunk(content="failed at line 13."),
        _chunk(finish="stop"),
    ]
    response = _client_with(chunks).chat([{"role": "user", "content": "go"}])
    assert response.content == "The build failed at line 13."
    assert not response.wants_tools
    assert response.stats.ttft_s is not None
    assert response.stats.total_s >= response.stats.ttft_s


def test_malformed_tool_arguments_do_not_crash_the_client():
    chunks = [
        _chunk(tool_deltas=[_tool_delta(0, "a", "read_file", "{not json")]),
        _chunk(finish="tool_calls"),
    ]
    response = _client_with(chunks).chat([{"role": "user", "content": "go"}])
    assert response.tool_calls[0].arguments == {}


def test_assistant_message_round_trips_tool_calls():
    response = ChatResponse(
        tool_calls=[ToolCall.from_parts("c1", "run_test", '{"name_filter": "ring"}')]
    )
    message = response.as_assistant_message()
    assert message["tool_calls"][0]["function"]["name"] == "run_test"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {
        "name_filter": "ring"
    }


# ----------------------------------------------------------- thinking mode


def test_reasoning_is_stripped_from_the_answer():
    """Qwen3 thinking must never reach the orchestrator as the answer."""
    from local_agent.llm.client import split_reasoning

    answer, reasoning = split_reasoning(
        "<think>Okay, the user wants exactly one line, so I should not "
        "overthink this.</think>LOCAL QWEN 8B ONLINE"
    )
    assert answer == "LOCAL QWEN 8B ONLINE"
    assert "overthink" in reasoning


def test_bracketed_thinking_is_also_stripped():
    from local_agent.llm.client import split_reasoning

    answer, reasoning = split_reasoning(
        "[Start thinking]\nreasoning here\n[End thinking]\n\nLOCAL QWEN 8B ONLINE"
    )
    assert answer == "LOCAL QWEN 8B ONLINE"
    assert "reasoning here" in reasoning


def test_unterminated_thinking_yields_no_answer_rather_than_reasoning():
    """Running out of budget mid-thought must not be mistaken for an answer."""
    from local_agent.llm.client import split_reasoning

    answer, reasoning = split_reasoning("<think>I will start by considering")
    assert answer == ""
    assert "considering" in reasoning


def test_plain_answers_are_untouched():
    from local_agent.llm.client import split_reasoning

    assert split_reasoning("ring_buffer.cpp:13 uses count") == (
        "ring_buffer.cpp:13 uses count", ""
    )
    assert split_reasoning("") == ("", "")


def test_thinking_cost_is_recorded_separately(sandbox):
    import types

    from local_agent.config import ModelConfig
    from local_agent.llm.client import OpenAICompatibleClient

    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks

        def __iter__(self):
            return iter(self._chunks)

    def chunk(content):
        delta = types.SimpleNamespace(content=content, tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
            usage=None,
        )

    client = OpenAICompatibleClient(ModelConfig(stream=True, thinking=False))
    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=lambda **kw: _Stream([
                    chunk("<think>"), chunk("a lot of deliberation"),
                    chunk("</think>"), chunk("READY"),
                ])
            )
        )
    )
    response = client.chat([{"role": "user", "content": "Reply with exactly: READY"}])
    assert response.content == "READY"
    assert response.stats.thinking_detected is True
    assert response.stats.thinking_chars > 20
    assert response.stats.as_dict()["thinking_chars"] > 20


def _streaming_client(create, *, thinking=False):
    """A client wired to a fake transport. `create` receives the request kwargs."""
    from local_agent.config import ModelConfig
    from local_agent.llm.client import OpenAICompatibleClient

    client = OpenAICompatibleClient(ModelConfig(stream=True, thinking=thinking))
    client._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)
        )
    )
    return client


def _text_chunks(*texts):
    out = []
    for text in texts:
        delta = types.SimpleNamespace(content=text, tool_calls=None)
        out.append(
            types.SimpleNamespace(
                choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
                usage=None,
            )
        )
    return out


# ---------------------------------------------- thinking: requested vs observed
# Three separate facts. Collapsing them would let a silent compatibility retry
# be reported as an effective policy, and every latency, token and energy
# comparison made afterwards would be measuring something else.


def test_call_stats_thinking_effective_is_observational():
    assert CallStats(thinking_detected=True).thinking_effective == "on"
    assert CallStats(completion_tokens=12).thinking_effective == "off"
    assert CallStats(thinking_chars=40).thinking_effective == "off"
    # Nothing came back at all, so nothing is claimed.
    assert CallStats().thinking_effective == "unknown"
    assert CallStats(thinking_detected=True).as_dict()["thinking_effective"] == "on"


def test_thinking_control_is_marked_accepted_when_the_server_takes_it():
    seen: list[dict] = []

    def create(**kw):
        seen.append(kw)
        return iter(_text_chunks("READY"))

    client = _streaming_client(create, thinking=False)
    stats = client.chat([{"role": "user", "content": "x"}]).stats

    assert seen[0]["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert stats.thinking_requested is False
    assert stats.thinking_control_accepted is True


def test_thinking_control_rejection_is_recorded_not_swallowed():
    """The retry is fine. Pretending the policy was applied is not."""
    seen: list[dict] = []

    def create(**kw):
        seen.append(kw)
        if "chat_template_kwargs" in kw.get("extra_body", {}):
            raise ValueError("unrecognised request argument: chat_template_kwargs")
        return iter(_text_chunks("<think>", "narrating away", "</think>", "READY"))

    client = _streaming_client(create, thinking=False)
    response = client.chat([{"role": "user", "content": "x"}])

    assert "chat_template_kwargs" not in seen[-1]["extra_body"]
    assert response.content == "READY"
    assert response.stats.thinking_requested is False
    assert response.stats.thinking_control_accepted is False
    # Asked for off, observed on. The run has to carry that, not hide it.
    assert response.stats.thinking_effective == "on"

    # And it is never asked for again on this client.
    seen.clear()
    client.chat([{"role": "user", "content": "y"}])
    assert "chat_template_kwargs" not in seen[0]["extra_body"]


def test_run_metrics_keeps_requested_accepted_and_observed_apart():
    from local_agent.agent.state import RunMetrics

    m = RunMetrics()
    m.observe_call(CallStats(total_s=1.0, completion_tokens=5,
                             thinking_requested=False,
                             thinking_control_accepted=True))
    assert m.thinking_effective == "off"
    assert m.thinking_policy_honoured is True

    # One rejection taints the whole run and never upgrades back.
    m.observe_call(CallStats(total_s=1.0, completion_tokens=5,
                             thinking_requested=False,
                             thinking_control_accepted=False,
                             thinking_detected=True, thinking_chars=200))
    m.observe_call(CallStats(total_s=1.0, completion_tokens=5,
                             thinking_requested=False,
                             thinking_control_accepted=True))
    assert m.thinking_control_accepted is False

    # One call that thought means the run thought.
    assert m.thinking_effective == "on"
    assert m.thinking_policy_honoured is False

    out = m.as_dict()
    assert out["thinking_requested"] is False
    assert out["thinking_control_accepted"] is False
    assert out["thinking_effective"] == "on"
    assert out["thinking_policy_honoured"] is False


def test_run_metrics_claims_nothing_when_nothing_was_observed():
    from local_agent.agent.state import RunMetrics

    m = RunMetrics()
    assert m.thinking_effective == "unknown"
    assert m.thinking_policy_honoured is None

    m.observe_call(CallStats(total_s=0.1, thinking_requested=True))
    assert m.thinking_effective == "unknown"
    assert m.thinking_policy_honoured is None


# ------------------------------------------------ SDK contract and provenance


def test_request_uses_only_sdk_keywords():
    """Anything the openai SDK does not know as a keyword raises TypeError.

    The fakes here accept **kw and would never notice. The real SDK would, and
    an earlier version of the client put chat_template_kwargs at the top level,
    which would have tripped the compatibility fallback on the very first real
    call and silently run with thinking at the server default.
    """
    from local_agent.llm.client import OpenAICompatibleClient

    seen: list[dict] = []
    client = _streaming_client(lambda **kw: (seen.append(kw), iter(_text_chunks("x")))[1],
                               thinking=False)
    client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function",
                 "function": {"name": "t", "parameters": {"type": "object", "properties": {}}}}])
    unknown = set(seen[0]) - OpenAICompatibleClient.SDK_KEYWORDS
    assert not unknown, unknown
    # and the non-standard fields are where the SDK will forward them
    body = seen[0]["extra_body"]
    assert body["top_k"] == 40 and body["min_p"] == 0.05
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen[0]["top_p"] == 0.95


def test_sampler_is_sent_and_in_identity():
    from local_agent.config import ModelConfig

    cfg = ModelConfig(top_k=20, top_p=0.9, min_p=0.0)
    identity = cfg.identity()
    assert (identity["top_k"], identity["top_p"], identity["min_p"]) == (20, 0.9, 0.0)


def test_provenance_and_timings_are_read_when_present_and_none_when_absent():
    # blocking, present
    message = types.SimpleNamespace(content="ok", tool_calls=None, reasoning_content=None)
    completion = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
        usage=types.SimpleNamespace(prompt_tokens=13, completion_tokens=2,
                                    prompt_tokens_details=types.SimpleNamespace(cached_tokens=12)),
        system_fingerprint="b10816-427291b5b",
        timings=types.SimpleNamespace(prompt_ms=351.4, predicted_ms=57.4, cache_n=12),
    )
    from local_agent.config import ModelConfig
    from local_agent.llm.client import OpenAICompatibleClient

    client = OpenAICompatibleClient(ModelConfig(stream=False))
    client._client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=lambda **kw: completion)))
    stats = client.chat([{"role": "user", "content": "x"}]).stats
    assert stats.system_fingerprint == "b10816-427291b5b"
    assert stats.server_prompt_ms == 351.4
    assert stats.server_predicted_ms == 57.4
    assert stats.server_cache_n == 12
    assert stats.as_dict()["system_fingerprint"] == "b10816-427291b5b"

    # streaming, present only on the final usage chunk (as llama.cpp sends it)
    final = types.SimpleNamespace(
        choices=[],
        usage=types.SimpleNamespace(prompt_tokens=324, completion_tokens=23,
                                    prompt_tokens_details=types.SimpleNamespace(cached_tokens=323)),
        system_fingerprint="b10816-427291b5b",
        timings=types.SimpleNamespace(prompt_ms=72.7, predicted_ms=1352.3, cache_n=323),
    )
    client = _streaming_client(lambda **kw: iter(_text_chunks("READY") + [final]))
    stats = client.chat([{"role": "user", "content": "x"}]).stats
    assert stats.system_fingerprint == "b10816-427291b5b"
    assert stats.server_cache_n == 323
    assert stats.cached_tokens == 323

    # absent: None everywhere, never zero
    client = _streaming_client(lambda **kw: iter(_text_chunks("READY")))
    stats = client.chat([{"role": "user", "content": "x"}]).stats
    assert stats.system_fingerprint is None
    assert stats.server_prompt_ms is None
    assert stats.server_cache_n is None


def test_transport_failures_become_a_typed_error_not_a_traceback():
    from local_agent.llm.models import LLMTransportError

    def refuse(**kw):
        raise ConnectionError("Failed to connect to 127.0.0.1:8080")

    client = _streaming_client(refuse)
    try:
        client.chat([{"role": "user", "content": "x"}])
    except LLMTransportError as exc:
        assert exc.cause == "ConnectionError"
        assert "127.0.0.1:8080" in str(exc)
    else:
        raise AssertionError("expected LLMTransportError")

    # dying mid-stream is the same class of fact
    def die_midway(**kw):
        yield _text_chunks("REA")[0]
        raise OSError("connection reset")

    client = _streaming_client(lambda **kw: die_midway(**kw))
    try:
        client.chat([{"role": "user", "content": "x"}])
    except LLMTransportError as exc:
        assert "stream aborted" in str(exc)
    else:
        raise AssertionError("expected LLMTransportError")


def test_run_metrics_flags_a_fingerprint_that_disagrees_with_the_profile():
    from local_agent.agent.state import RunMetrics

    m = RunMetrics(expected_runtime_version="b10816-427291b5b")
    m.observe_call(CallStats(total_s=1, completion_tokens=2,
                             system_fingerprint="b10816-427291b5b",
                             server_prompt_ms=10.0, server_predicted_ms=20.0))
    assert m.identity_mismatch is False
    m.observe_call(CallStats(total_s=1, completion_tokens=2,
                             system_fingerprint="b10900-deadbeef",
                             server_prompt_ms=5.0))
    assert m.identity_mismatch is True
    out = m.as_dict()
    assert out["fingerprints_observed"] == ["b10816-427291b5b", "b10900-deadbeef"]
    assert out["server_prompt_ms_total"] == 15.0
    assert out["server_predicted_ms_total"] == 20.0

    # no expectation recorded: nothing is checked, nothing is claimed
    m = RunMetrics()
    m.observe_call(CallStats(total_s=1, completion_tokens=2, system_fingerprint="anything"))
    assert m.identity_mismatch is False
    # no fingerprint reported at all: None, not an empty list
    assert RunMetrics().as_dict()["fingerprints_observed"] is None


def test_configuration_identity_is_complete():
    """Every number gets quoted next to the configuration that produced it."""
    from local_agent.config import MODEL_PRESETS

    identity = MODEL_PRESETS["nuc-llama-30b"].identity()
    assert identity["runtime"] == "llamacpp"
    assert identity["runtime_version"] == "b10816-427291b5b"
    assert identity["quant"] == "UD-Q4_K_XL"
    assert identity["thinking"] is False
    assert set(identity) == {
        "model", "device", "runtime", "runtime_version", "quant", "tool_parser",
        "thinking", "context_budget_tokens", "max_tool_result_tokens",
        "temperature", "top_k", "top_p", "min_p",
        # Every clock, so nobody has to guess which one aborts what. The
        # deadline is the whole request; the stall pair is the throughput
        # floor that kills a live-but-useless stream long before it.
        "connect_timeout_s", "read_timeout_s", "request_deadline_s",
        "stall_tok_s", "stall_window_s", "stream",
    }


def test_a_run_can_name_the_source_that_produced_it():
    """Feature markers say a feature is present. They do not identify a tree."""
    from local_agent.provenance import package_identity, source_sha256

    ident = package_identity()
    assert len(ident["source_sha256"]) == 64
    assert ident["source_sha256"] == source_sha256()   # stable within a tree
    assert ident["package_source"] in {"stamp", "git", "unknown"}
    if ident["package_commit"] is not None:
        assert len(ident["package_commit"]) == 40


def test_the_source_hash_ignores_generated_state():
    """A doctor run drops .local-agent into the fixture. If that changed the
    hash, two runs of the same package would report different provenance."""
    from local_agent.provenance import source_sha256

    import local_agent.provenance as prov

    before = source_sha256()
    junk = prov._ROOT / "benchmark_fixture" / "cpp_project" / ".local-agent" / "runs" / "x"
    junk.mkdir(parents=True, exist_ok=True)
    try:
        (junk / "combined.log").write_text("noise")
        assert source_sha256() == before
    finally:
        import shutil
        shutil.rmtree(prov._ROOT / "benchmark_fixture" / "cpp_project" / ".local-agent",
                      ignore_errors=True)


def test_the_source_hash_covers_everything_that_can_change_a_result():
    """An edit to the qualification gate once produced an identical hash,
    because measurement was not hashed. The gate decides whether a run starts."""
    from local_agent.provenance import _files

    rels = {str(p).split("local-code-agent/")[-1] for p in _files()}
    for expected in ("measurement/qualify_server.py", "measurement/run_experiment.sh",
                     "evaluation/run_evaluation.py", "evaluation/task_contracts.py",
                     "local_agent/agent/orchestrator.py",
                     "skills/diagnose-test-failure/SKILL.md"):
        assert any(r.endswith(expected) for r in rels), expected


def test_the_model_facing_contract_has_its_own_fingerprint():
    """source_sha256 moves for any change. This moves only when what the model
    is TOLD changes, which is what decides whether an old dataset can be
    re-scored like for like."""
    from local_agent.provenance import base_prompt_sha256, package_identity

    ident = package_identity()
    assert ident["base_prompt_sha256"] == base_prompt_sha256()
    assert len(ident["base_prompt_sha256"]) == 64
    assert ident["base_prompt_sha256"] != ident["source_sha256"]


def test_the_fingerprint_covers_more_than_the_system_prompt():
    """It used to hash SYSTEM_PROMPT and nothing else, and two model-facing
    changes slipped under it: every tool result gained an `evidence_id` field,
    and the answer contract narrowed to canonical ids only. A dataset from that
    tree advertised comparability it did not have.

    "The base prompt" is not one string. It is everything the model is handed
    and everything its answer is measured against.
    """
    import hashlib

    from local_agent.agent.context import SYSTEM_PROMPT
    from local_agent.provenance import base_prompt_sha256

    fingerprint = base_prompt_sha256()
    assert fingerprint != "unavailable-no-source", \
        "source has to be readable for this number to mean anything"
    assert fingerprint != hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(), \
        "hashing the prompt alone is the defect, not the fix"


def test_the_fingerprint_moves_when_any_covered_surface_moves(monkeypatch):
    """Each covered surface is pinned individually, so removing one from the
    hash fails here rather than silently shrinking what the number promises."""
    from local_agent.agent import context as ctxmod
    from local_agent.agent.contracts import Orchestrator
    from local_agent.provenance import base_prompt_sha256

    before = base_prompt_sha256()

    monkeypatch.setattr(ctxmod, "SYSTEM_PROMPT", ctxmod.SYSTEM_PROMPT + "\n")
    assert base_prompt_sha256() != before, "the system prompt is not covered"
    monkeypatch.undo()

    # The source of each function is hashed, so a genuinely different function
    # object in its place is the closest honest stand-in for an edit.
    for owner, attr, label in (
        (ctxmod, "build_system_message", "what the model is told"),
        (ctxmod, "build_skill_message", "the skill message"),
        (ctxmod, "tool_result_message", "the shape of every tool result"),
        (Orchestrator, "_execute", "the tool result payload"),
        (Orchestrator, "_accept_answer", "the answer contract"),
    ):
        def replacement(*args, **kwargs):  # noqa: ANN002, ANN003
            raise NotImplementedError("a different function body")

        monkeypatch.setattr(owner, attr, replacement)
        assert base_prompt_sha256() != before, f"{label} is not covered ({attr})"
        monkeypatch.undo()

    assert base_prompt_sha256() == before, "and nothing leaked between cases"


def test_tool_schemas_are_recorded_per_row_and_not_in_the_fingerprint():
    """Tool names, descriptions and schemas are model-facing, and are
    deliberately outside this number.

    They are the independent variable: they differ by condition on purpose, so
    one per-run value cannot describe them without lying about one of the arms.
    Every row carries `tool_schema_hash` over the exact toolset that row was
    offered, which is the honest place for it. This test exists so nobody
    "fixes" the omission later without reading why it is there.
    """
    import inspect

    from local_agent import provenance

    source = inspect.getsource(provenance.base_prompt_sha256)
    assert "tool_schema_hash" in source, \
        "the reason for the omission has to travel with the code"
    assert "registry" not in source.split('"""')[2], \
        "the registry must not creep into the per-run fingerprint"


def test_the_finishing_protocol_is_in_the_shared_prompt():
    """Not in the skills. It is infrastructure, and teaching it only in the
    treatment made the control unable to satisfy a contract nobody gave it."""
    from local_agent.agent.context import SYSTEM_PROMPT

    assert "submit_answer" in SYSTEM_PROMPT
    assert "reply in prose" not in SYSTEM_PROMPT


def test_the_instrument_declaration_is_outside_the_hashed_surface(tmp_path):
    """INSTRUMENT.json declares the hashes CI checks, so it must not be hashed
    itself. If it were, every update to it would invalidate the value it just
    declared and the check could never be satisfied.

    Same for `.github/`. CI configuration decides what runs in CI, not what the
    agent does, and folding it in would make the instrument hash churn on
    changes that cannot move a measured number.

    Proved by editing both and showing the hash does not move, rather than by
    reading `_HASHED` and trusting it.
    """
    import json
    from pathlib import Path

    from local_agent import provenance

    root = Path(provenance.__file__).resolve().parent.parent
    declaration = root / "INSTRUMENT.json"
    workflow = root / ".github" / "workflows" / "tests.yml"
    assert declaration.is_file(), "the declaration CI checks against has to exist"

    before = provenance.source_sha256()
    originals = {p: p.read_bytes() for p in (declaration, workflow) if p.is_file()}
    try:
        for path, blob in originals.items():
            path.write_bytes(blob + b"\n# scratch\n")
            assert provenance.source_sha256() == before, \
                f"{path.name} is inside the hashed surface and must not be"
    finally:
        for path, blob in originals.items():
            path.write_bytes(blob)
    assert provenance.source_sha256() == before


def test_the_declared_identity_matches_this_tree():
    """The same comparison CI makes, run locally, so drift is caught before a
    push rather than by a red build afterwards.

    A failure here is not a bug to work around. Either the change was meant, in
    which case update INSTRUMENT.json in the same commit and say what generation
    it opens, or it was not, in which case something altered measured behaviour
    by accident.
    """
    import json
    from pathlib import Path

    from local_agent import provenance

    root = Path(provenance.__file__).resolve().parent.parent
    declared = json.loads((root / "INSTRUMENT.json").read_text())

    assert declared["source_sha256"] == provenance.source_sha256(), \
        "source_sha256 has drifted from INSTRUMENT.json"
    assert declared["base_prompt_sha256"] == provenance.base_prompt_sha256(), \
        "base_prompt_sha256 has drifted from INSTRUMENT.json, which ends a generation"
