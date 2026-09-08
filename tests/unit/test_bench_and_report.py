"""Tests for the benchmark and the comparison report.

These are the two things that will be shown to other people, so they get tested
against synthetic data with known answers rather than trusted because they ran
without raising.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "measurement"))
sys.path.insert(0, str(REPO / "evaluation"))

from local_agent.llm.models import CallStats, ChatResponse  # noqa: E402


class FakeModel:
    """A model with known, linear timing, so the maths can be checked exactly.

    prefill runs at `prefill_rate` tokens per second and decode at
    `decode_rate`, with a fixed cost for a cache hit on a repeated prompt.
    """

    def __init__(self, prefill_rate=500.0, decode_rate=10.0, chars_per_token=4):
        self.prefill_rate = prefill_rate
        self.decode_rate = decode_rate
        self.chars_per_token = chars_per_token
        self._seen: set[str] = set()
        self.calls = 0

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls += 1
        text = "".join(str(m.get("content") or "") for m in messages)
        prompt_tokens = max(1, len(text) // self.chars_per_token)
        cached = prompt_tokens if text in self._seen else 0
        self._seen.add(text)

        uncached = prompt_tokens - cached
        ttft = uncached / self.prefill_rate + 0.05
        completion = min(max_tokens or 32, 32)
        total = ttft + (completion - 1) / self.decode_rate

        return ChatResponse(
            content="ok " * completion,
            stats=CallStats(
                total_s=total,
                ttft_s=ttft,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion,
                cached_tokens=cached,
                streamed=True,
            ),
        )


def test_benchmark_recovers_the_rates_it_was_given():
    from benchmark_model import render_markdown, run_benchmark, summarise

    model = FakeModel(prefill_rate=500.0, decode_rate=10.0)
    result = run_benchmark(
        model,
        endpoint="fake://",
        model="fake-model",
        device="TEST",
        prompt_chars=[1000, 4000, 16000],
        repeats=2,
        decode_tokens=32,
    )
    summary = summarise(result)

    assert len(summary["prefill"]) == 3
    # The measured prefill rate should land near the rate the fake was built
    # with. It reads slightly low because of the fixed 50 ms overhead per call.
    biggest = summary["prefill"][-1]
    assert 400 < biggest["median_prefill_tok_s"] <= 500
    assert 9 < summary["median_decode_tok_s"] <= 10.5

    # Longer prompts must take longer to first token. If this ever inverts, the
    # measurement is wrong, not the hardware.
    ttfts = [row["median_ttft_s"] for row in summary["prefill"]]
    assert ttfts == sorted(ttfts)

    report = render_markdown(summary)
    assert "prefill tok/s" in report
    assert "Projected agent cost" in report


def test_benchmark_detects_a_prefix_cache_hit():
    from benchmark_model import run_benchmark, summarise

    summary = summarise(
        run_benchmark(
            FakeModel(prefill_rate=500.0, decode_rate=10.0),
            endpoint="fake://",
            model="fake-model",
            device="TEST",
            prompt_chars=[8000],
            repeats=1,
            decode_tokens=16,
        )
    )
    cache = summary["cache"]
    assert cache["warm_cached_tokens"] > 0
    assert cache["speedup"] > 5
    # Rewriting the middle of the prompt loses the hit entirely, which is
    # exactly what a context compaction does.
    assert cache["mutated_ttft_s"] > cache["warm_ttft_s"] * 5


def test_warmup_is_excluded_from_the_statistics():
    from benchmark_model import run_benchmark, summarise

    result = run_benchmark(
        FakeModel(),
        endpoint="fake://", model="m", device="TEST",
        prompt_chars=[1000], repeats=1, decode_tokens=8,
    )
    assert result.samples[0].label == "warmup"
    assert all(not row["prompt_tokens"] == 0 for row in summarise(result)["prefill"])
    assert "warm-up" in summarise(result)["notes"][0]


# ----------------------------------------------------------------- compare


def _fake_eval_file(tmp_path: Path, label: str, score: float, seconds: float,
                    ttft: float) -> Path:
    from task_contracts import CASES

    rows = []
    for case in CASES:
        rows.append(
            {
                "case": case.name,
                "scenario": case.scenario,
                "score": score,
                "weight": case.weight,
                "checks": {"whatever": score == 1.0},
                "tool_calls": 5,
                "elapsed_s": seconds,
                "metrics": {
                    "median_ttft_s": ttft,
                    "median_decode_tok_s": 12.0,
                    "cache_hit_ratio": 0.7,
                    "compactions": 0,
                },
                "answer": "answer",
            }
        )
    path = tmp_path / f"{label}.json"
    path.write_text(
        json.dumps(
            {
                "label": label,
                "model": f"model-{label}",
                "device": "CPU" if "cpu" in label else "NPU",
                "overall": score,
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_comparison_calls_a_low_scorer_dead(tmp_path):
    from compare_datasets import compare, load, render

    good = load(_fake_eval_file(tmp_path, "cpu-30b", 0.95, 60.0, 4.0))
    bad = load(_fake_eval_file(tmp_path, "npu-8b", 0.3, 20.0, 1.2))
    report = compare([good, bad])

    verdicts = {v["label"]: v for v in report["verdicts"]}
    assert "kill threshold" in verdicts["npu-8b"]["verdict"]
    assert "kill threshold" not in verdicts["cpu-30b"]["verdict"]
    assert verdicts["cpu-30b"]["diagnostic_score"] == 0.95

    text = render(report, [good, bad])
    assert "cpu-30b" in text and "npu-8b" in text
    assert "diagnostic case" in text


def test_comparison_separates_accurate_but_slow_from_usable(tmp_path):
    from compare_datasets import compare, load

    slow = load(_fake_eval_file(tmp_path, "cpu-slow", 0.9, 300.0, 25.0))
    quick = load(_fake_eval_file(tmp_path, "npu-quick", 0.9, 40.0, 1.5))
    verdicts = {v["label"]: v for v in compare([slow, quick])["verdicts"]}

    assert "batch" in verdicts["cpu-slow"]["verdict"]
    assert "sit in the loop" in verdicts["npu-quick"]["verdict"]


def test_comparison_covers_every_case_from_both_runs(tmp_path):
    from compare_datasets import compare, load
    from task_contracts import CASES

    a = load(_fake_eval_file(tmp_path, "a", 1.0, 10.0, 1.0))
    b = load(_fake_eval_file(tmp_path, "b", 0.5, 10.0, 1.0))
    report = compare([a, b])
    assert {e["case"] for e in report["cases"]} == {c.name for c in CASES}
    assert sum(1 for e in report["cases"] if e["diagnostic"]) == 4


# ------------------------------------------------------------------ ledger


def _row(case, outcome, score, seconds, cheap_calls, strong_calls):
    return {
        "case": case,
        "score": score,
        "succeeded": outcome in ("pass", "escalated_pass") and score >= 0.75,
        "outcome": outcome,
        "elapsed_s": seconds,
        "routing": {
            "escalated": outcome.startswith("escalated"),
            "escalation_reason": "no answer was produced" if outcome.startswith("escalated") else None,
            "available_tiers": ["cheap", "strong"],
        },
        "metrics": {
            "tiers": {
                "by_tier": {
                    "cheap": {"calls": cheap_calls},
                    "strong": {"calls": strong_calls},
                }
            }
        },
    }


def test_ledger_counts_tasks_not_model_calls():
    from run_evaluation import build_ledger

    rows = [
        _row("a", "pass", 1.0, 10.0, 6, 0),          # chatty but one task
        _row("b", "pass", 1.0, 12.0, 2, 0),
        _row("c", "escalated_pass", 1.0, 40.0, 3, 4),
        _row("d", "escalated_fail", 0.2, 55.0, 3, 4),
        _row("e", "blocked", 0.0, 3.0, 1, 0),
    ]
    ledger = build_ledger(rows)

    assert ledger["tasks"] == 5
    assert ledger["blocked"] == 1 and ledger["blocked_cases"] == ["e"]
    assert ledger["attempted"] == 4
    assert ledger["cheap_only_success"] == 2
    assert ledger["escalated_success"] == 1
    assert ledger["local_success"] == 3
    assert ledger["cheap_alone_rate"] == 0.5
    assert ledger["local_success_rate"] == 0.75
    assert ledger["cloud_required_rate"] == 0.25
    # Task counts and call counts must not be confused with each other.
    assert ledger["calls_by_tier"] == {"cheap": 15, "strong": 8}
    assert ledger["cheap_call_share"] == round(15 / 23, 3)


def test_blocked_tasks_never_count_against_the_model():
    from run_evaluation import build_ledger

    ledger = build_ledger([
        _row("a", "pass", 1.0, 5.0, 2, 0),
        _row("b", "blocked", 0.0, 1.0, 1, 0),
        _row("c", "blocked", 0.0, 1.0, 1, 0),
    ])
    # One attempted task, one success, so 100 percent, not 33 percent.
    assert ledger["attempted"] == 1
    assert ledger["cheap_alone_rate"] == 1.0
    assert ledger["cloud_required_rate"] == 0.0


def test_ledger_renders_without_escalations():
    from run_evaluation import build_ledger, render_ledger

    text = render_ledger(build_ledger([_row("a", "pass", 1.0, 5.0, 2, 0)]))
    assert "Cheap tier alone" in text
    assert "no escalations" in text


# ------------------------------------------------------------- qualification


class ConformantServer:
    """A well-behaved server: streams, reports usage, caches, calls tools."""

    def __init__(self, garble_above_tokens=None, cache_with_tools=True):
        self.garble_above = garble_above_tokens
        self.cache_with_tools = cache_with_tools
        self._seen = set()

    def probe(self):
        return {"served_models": ["m"], "configured_model_present": True}

    def chat(self, messages, tools=None, max_tokens=None):
        from local_agent.llm.models import CallStats, ChatResponse, ToolCall

        text = "".join(str(m.get("content") or "") for m in messages)
        prompt_tokens = max(1, len(text) // 4)
        # Tool-bearing requests get their own cache namespace, the way a server
        # that serialises tools into the prompt would.
        key = text + ("|tools" if tools else "")
        if tools and not self.cache_with_tools:
            cached = 0            # schema serialisation varies, prefix never matches
        else:
            cached = prompt_tokens if key in self._seen else 0
        self._seen.add(key)
        ttft = (prompt_tokens - cached) / 800.0 + 0.02

        names = {t["function"]["name"] for t in (tools or [])}
        if "submit_answer" in names and len(names) == 1:
            calls = [ToolCall.from_parts("c1", "submit_answer",
                                         '{"claim": "diagnosis", "summary": "done"}')]
            content = ""
        elif names:
            calls = [ToolCall.from_parts("c1", "git_status", "{}")]
            content = ""
        else:
            calls = []
            garbled = self.garble_above is not None and prompt_tokens > self.garble_above
            content = "@@@ !! ###" if garbled else "pomegranate"

        return ChatResponse(
            content=content, tool_calls=calls,
            stats=CallStats(total_s=ttft + 0.5, ttft_s=ttft,
                            prompt_tokens=prompt_tokens, completion_tokens=8,
                            cached_tokens=cached, streamed=True),
        )


def _schemas():
    from pathlib import Path as _P

    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    registry, _, _ = build_registry(
        load_repo_config(_P(REPO / "benchmark_fixture" / "cpp_project"))
    )
    return registry.schemas(["git_status", "read_file", "build_target", "submit_answer"])


def test_qualification_passes_a_conformant_server():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ConformantServer(), MODEL_PRESETS["ptl-npu-8b"],
                          _schemas(), [1000, 4000])
    assert q.failed == [], [c.name for c in q.failed]
    names = {c.name for c in q.checks}
    assert "prefix cache, with tools" in names
    assert "tool schema serialisation stable" in names
    assert "context 4000 tokens" in names


def test_qualification_catches_a_context_cliff():
    """The exact NPU failure mode: garbage output instead of an error."""
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ConformantServer(garble_above_tokens=1500),
                          MODEL_PRESETS["ptl-npu-8b"], _schemas(),
                          [1000, 4000, 8000])
    failures = {c.name: c for c in q.failed}
    assert "context 4000 tokens" in failures
    assert "GARBLED" in failures["context 4000 tokens"].detail
    # It stops probing once output has gone bad rather than wasting the rest.
    assert not any(c.name == "context 8000 tokens" for c in q.checks)


def test_an_agent_capable_profile_that_cannot_call_tools_fails():
    """Not a downgrade to "unsupported optional feature". A failure."""
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    class NoToolServer(ConformantServer):
        def chat(self, messages, tools=None, max_tokens=None):
            response = super().chat(messages, tools=None, max_tokens=max_tokens)
            response.tool_calls = []
            response.content = response.content or "I would run git status."
            return response

    q = run_qualification(NoToolServer(), MODEL_PRESETS["nuc-llama-30b"],
                          _schemas(), [1000])
    failed = {c.name: c for c in q.failed}
    assert "tool call round trip (streaming)" in failed
    # And the raw material is kept so it can be reproduced outside local-agent.
    assert failed["tool call round trip (streaming)"].data.get("raw_request")
    assert failed["tool call round trip (streaming)"].data.get("raw_response")


def test_qualification_records_the_configuration_identity():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    cfg = MODEL_PRESETS["nuc-llama-30b"]
    run_qualification(ConformantServer(), cfg, _schemas(), [1000])
    identity = cfg.identity()
    assert identity["runtime"] == "llamacpp"
    assert identity["runtime_version"] == "b10816-427291b5b"
    assert identity["device"] == "CPU"
    assert identity["model"] == "qwen3-coder-30b"


class ThinkingServer(ConformantServer):
    """A server that takes the thinking control, or does not, and then ignores it."""

    def __init__(self, *, accepted, thinks, **kw):
        super().__init__(**kw)
        self.accepted = accepted
        self.thinks = thinks

    def chat(self, messages, tools=None, max_tokens=None):
        response = super().chat(messages, tools=tools, max_tokens=max_tokens)
        response.stats.thinking_requested = False
        response.stats.thinking_control_accepted = self.accepted
        response.stats.thinking_detected = self.thinks
        response.stats.thinking_chars = 900 if self.thinks else 0
        return response


def test_qualification_fails_when_the_thinking_policy_is_not_honoured():
    """Asked for thinking off, got thinking on. Every number after this is suspect."""
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ThinkingServer(accepted=True, thinks=True),
                          MODEL_PRESETS["nuc-llama-30b"], _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["thinking policy honoured"].status == "FAIL"
    assert "accepted the control and ignored it" in checks["thinking policy honoured"].detail
    # Accepting the control is a separate fact and it is reported separately.
    assert checks["thinking control accepted"].status == "PASS"


def test_a_rejected_thinking_control_is_visible_not_swallowed():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ThinkingServer(accepted=False, thinks=True),
                          MODEL_PRESETS["nuc-llama-30b"], _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["thinking control accepted"].status == "WARN"
    assert "retried without it" in checks["thinking control accepted"].detail
    assert checks["thinking policy honoured"].status == "FAIL"


def test_an_honoured_thinking_policy_passes():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ThinkingServer(accepted=True, thinks=False),
                          MODEL_PRESETS["nuc-llama-30b"], _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["thinking control accepted"].status == "PASS"
    assert checks["thinking policy honoured"].status == "PASS"
    assert q.failed == [], [c.name for c in q.failed]


class FingerprintServer(ConformantServer):
    def __init__(self, fingerprint, **kw):
        super().__init__(**kw)
        self.fingerprint = fingerprint

    def chat(self, messages, tools=None, max_tokens=None):
        response = super().chat(messages, tools=tools, max_tokens=max_tokens)
        response.stats.system_fingerprint = self.fingerprint
        response.stats.server_prompt_ms = 100.0
        response.stats.server_predicted_ms = 50.0
        response.stats.server_cache_n = 0
        return response


def test_qualification_checks_the_runtime_fingerprint_against_the_profile():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    cfg = MODEL_PRESETS["nuc-llama-30b"]          # claims b10816-427291b5b
    q = run_qualification(FingerprintServer("b10816-427291b5b"), cfg, _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["runtime fingerprint matches profile"].status == "PASS"
    assert checks["server-side timings reported"].status == "PASS"

    q = run_qualification(FingerprintServer("b10900-somebody-else"), cfg, _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["runtime fingerprint matches profile"].status == "FAIL"
    assert "b10900-somebody-else" in checks["runtime fingerprint matches profile"].detail
    assert q.failed  # a swapped server fails qualification outright


class AlreadyWarmServer(ConformantServer):
    """Reports every prompt as fully cached, as a server that still holds the
    previous qualification run's prompts would for a repeated filler."""

    def chat(self, messages, tools=None, max_tokens=None):
        response = super().chat(messages, tools=tools, max_tokens=max_tokens)
        response.stats.cached_tokens = max(0, response.stats.prompt_tokens - 1)
        response.stats.ttft_s = 0.08
        response.stats.total_s = 0.6
        return response


def test_a_cold_probe_that_was_already_cached_does_not_pass():
    """1.6x on a warm-versus-warmer comparison is not a validated cache."""
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(AlreadyWarmServer(), MODEL_PRESETS["nuc-llama-30b"], _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["prefix cache, no tools"].status == "WARN"
    assert "already cached" in checks["prefix cache, no tools"].detail
    assert checks["prefix cache, with tools"].status == "FAIL"
    assert "already cached" in checks["prefix cache, with tools"].detail


def test_a_warm_tool_header_is_not_a_cold_probe_failure():
    """The tool schemas sit in front of every prompt, so on a server that has
    already answered one tool call the cold probe is partially warm by
    construction. That is the design working, not a defect.

    This is the check that stopped the second controlled probe: 743 of 2722
    tokens cached on a "cold" leg, which is exactly the rendered tool schemas
    and says nothing about whether the body under test was cached.
    """
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    class WarmHeaderServer(ConformantServer):
        """Caches a fixed-size header for any request that carries tools, and
        nothing else. Exactly the shape of the real failure."""

        HEADER = 743

        def chat(self, messages, tools=None, max_tokens=None):
            response = super().chat(messages, tools=tools, max_tokens=max_tokens)
            if tools and response.stats.cached_tokens is not None:
                # The schemas are rendered into the prompt, so they count
                # towards its length as well as being permanently warm.
                repeat = response.stats.cached_tokens > 0
                response.stats.prompt_tokens += self.HEADER
                response.stats.cached_tokens = (
                    response.stats.prompt_tokens - 1 if repeat
                    else min(self.HEADER, response.stats.prompt_tokens - 1)
                )
            return response

    q = run_qualification(WarmHeaderServer(), MODEL_PRESETS["nuc-llama-30b"],
                          _schemas(), [1000])
    checks = {c.name: c for c in q.checks}
    assert checks["prefix cache, with tools"].status == "PASS", \
        checks["prefix cache, with tools"].detail
    # And the header is reported as a number, not hidden inside a verdict.
    assert checks["prefix cache, with tools"].data["shared_prefix_tokens"] > 0


def test_cache_verdict_uses_counts_first_and_timing_second():
    from qualify_server import _cache_verdict

    class S:  # minimal stand-in for a ChatResponse
        def __init__(self, cached, prompt, ttft):
            self.stats = CallStats(ttft_s=ttft, total_s=ttft + 0.5, prompt_tokens=prompt,
                                   completion_tokens=2, cached_tokens=cached, streamed=True)

    # counts prove reuse, timing agrees: PASS
    assert _cache_verdict(S(3, 3000, 27.0), S(2999, 3000, 0.09), hard=True)[0] == "PASS"
    # counts prove reuse but timing is flat (a fast box, a tiny prompt): WARN, not FAIL
    assert _cache_verdict(S(3, 3000, 0.12), S(2999, 3000, 0.10), hard=True)[0] == "WARN"
    # counts say no reuse even though timing looks fine: FAIL, timing is not the oracle
    assert _cache_verdict(S(0, 3000, 27.0), S(0, 3000, 0.09), hard=True)[0] == "FAIL"
    # a cold leg holding exactly the shared header is fine; one holding the
    # body under test is not, and the header is what tells them apart
    assert _cache_verdict(S(743, 3000, 27.0), S(2999, 3000, 0.09), hard=True,
                          shared_prefix_tokens=743)[0] == "PASS"
    assert _cache_verdict(S(2000, 3000, 27.0), S(2999, 3000, 0.09), hard=True,
                          shared_prefix_tokens=743)[0] == "FAIL"
    # no counts reported at all: timing is all there is, and the detail says so
    status, detail, _ = _cache_verdict(S(None, 3000, 27.0), S(None, 3000, 0.09), hard=True)
    assert status == "PASS" and "timing only" in detail


def test_qualification_catches_tools_breaking_the_prefix_cache():
    from local_agent.config import MODEL_PRESETS
    from qualify_server import run_qualification

    q = run_qualification(ConformantServer(cache_with_tools=False),
                          MODEL_PRESETS["ptl-npu-8b"], _schemas(), [1000])
    failures = {c.name for c in q.failed}
    assert "prefix cache, with tools" in failures
    assert "prefix cache, no tools" not in failures
