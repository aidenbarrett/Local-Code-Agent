from __future__ import annotations

import json

from local_agent.agent.context import ContextManager
from local_agent.llm.protocol import CallStats


def _tool_message(index: int, payload_chars: int = 4000) -> dict:
    return {
        "role": "tool",
        "tool_call_id": f"c{index}",
        "name": "build_target",
        "content": json.dumps({"ok": False, "data": {"blob": "x" * payload_chars}}),
    }


def test_context_is_append_only_until_compaction_is_needed():
    ctx = ContextManager(budget_tokens=100_000)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(6):
        ctx.append(_tool_message(i))
    snapshot = [dict(message) for message in ctx.for_request()]
    ctx.append(_tool_message(99))
    assert ctx.for_request()[: len(snapshot)] == snapshot
    assert ctx.compactions == []


def test_context_compaction_reclaims_space_and_preserves_recent_results():
    ctx = ContextManager(budget_tokens=3_000, keep_recent_tool_results=2)
    ctx.append({"role": "system", "content": "sys"})
    for i in range(12):
        ctx.append(_tool_message(i))
    event = ctx.compact()
    assert event is not None
    assert event.tokens_reclaimed > 0
    assert "x" * 100 in ctx.messages[-1]["content"]


def test_call_stats_split_client_observed_prefill_and_decode_without_inventing_missing_ttft():
    stats = CallStats(total_s=10.0, ttft_s=4.0, prompt_tokens=4000, completion_tokens=61, cached_tokens=0, streamed=True)
    assert stats.decode_s == 6.0
    assert stats.prefill_tok_s == 1000.0
    assert stats.decode_tok_s == 10.0

    unknown = CallStats(total_s=9.0, ttft_s=None, prompt_tokens=100, completion_tokens=50)
    assert unknown.decode_s is None
    assert unknown.decode_tok_s is None
    assert unknown.prefill_tok_s is None
