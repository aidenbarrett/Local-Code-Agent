#!/usr/bin/env python3
"""Model benchmark: prefill and decode measured separately, plus cache behaviour.

Tokens per second is a useless single number for an agent. An agent turn is a
long prompt and a short completion, so wall clock is dominated by prefill and
by whether the server's KV prefix cache hit. This measures those three things
and nothing else:

  1. prefill  - time to first token against a sweep of prompt lengths, with
                every prompt unique so the cache cannot help
  2. decode   - sustained tokens per second once generation is running
  3. cache    - the same long prompt sent twice, so you can see what a prefix
                hit is worth on this hardware

Then it projects those into "seconds per agent turn" and "seconds per task",
which is the number anyone at work will actually ask you about.

    python measurement/benchmark_model.py --profile nuc-cpu-30b --out bench-nuc.json
    python measurement/benchmark_model.py --profile ptl-npu-8b  --out bench-npu.json
    python measurement/benchmark_model.py --base-url http://127.0.0.1:8000/v3 --model X
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from local_agent.config import MODEL_PRESETS, ModelConfig  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402

# Filler that looks like the C++ and log text the agent actually carries, so the
# tokeniser behaves roughly the way it will in a real run. Random word order
# defeats any prefix cache between samples.
_VOCAB = (
    "void int const auto return template typename struct class namespace "
    "std vector string size_t buffer ring push pop capacity assert error "
    "warning undefined reference cmake ctest target link compile diagnostic "
    "src include tests build failed passed timeout segfault exit code line "
    "column severity message symbol expected token identifier scope"
).split()


def filler(rng: random.Random, approx_chars: int) -> str:
    out: list[str] = []
    length = 0
    while length < approx_chars:
        word = rng.choice(_VOCAB)
        out.append(word)
        length += len(word) + 1
    return " ".join(out)


@dataclass
class Sample:
    label: str
    prompt_chars: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    ttft_s: float | None
    total_s: float
    prefill_tok_s: float | None
    decode_tok_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BenchResult:
    endpoint: str
    model: str
    device: str
    host: dict[str, Any] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_label(self, prefix: str) -> list[Sample]:
        return [s for s in self.samples if s.label.startswith(prefix)]


def _host_info() -> dict[str, Any]:
    info = {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "python": platform.python_version(),
    }
    try:  # best effort, never fatal
        import os

        info["cpu_count"] = os.cpu_count()
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            info["ram_gb"] = round(total / 1e9, 1)
    except Exception:
        pass
    return info


def _measure(
    client: Any,
    label: str,
    prompt: str,
    max_tokens: int,
) -> Sample:
    messages = [
        {"role": "system", "content": "You are a terse assistant. Answer in one short sentence."},
        {"role": "user", "content": prompt},
    ]
    started = time.monotonic()
    response = client.chat(messages, tools=None, max_tokens=max_tokens)
    total = time.monotonic() - started
    stats = response.stats
    return Sample(
        label=label,
        prompt_chars=len(prompt),
        prompt_tokens=stats.prompt_tokens,
        completion_tokens=stats.completion_tokens,
        cached_tokens=stats.cached_tokens,
        ttft_s=stats.ttft_s,
        total_s=stats.total_s or total,
        prefill_tok_s=stats.prefill_tok_s,
        decode_tok_s=stats.decode_tok_s,
    )


def run_benchmark(
    client: Any,
    endpoint: str,
    model: str,
    device: str,
    prompt_chars: list[int],
    repeats: int,
    decode_tokens: int,
    seed: int = 1234,
) -> BenchResult:
    rng = random.Random(seed)
    result = BenchResult(endpoint=endpoint, model=model, device=device, host=_host_info())

    # Warm-up. First call pays model load, graph compile and blob cache misses,
    # and including it would make every number a lie.
    warm = _measure(client, "warmup", filler(rng, 400) + "\nSay ok.", 8)
    result.samples.append(warm)
    result.notes.append(
        f"warm-up call took {warm.total_s:.1f}s and is excluded from all statistics"
    )

    # 1. Prefill sweep. Unique prompt every time so nothing can be cached.
    for chars in prompt_chars:
        for attempt in range(repeats):
            prompt = (
                filler(rng, chars)
                + "\n\nIn one short sentence, what is the last word above?"
            )
            result.samples.append(
                _measure(client, f"prefill/{chars}", prompt, max_tokens=24)
            )

    # 2. Decode rate. Short prompt, long completion.
    for attempt in range(repeats):
        result.samples.append(
            _measure(
                client,
                "decode",
                "Explain RAII in C++ in detail, with a worked example.",
                max_tokens=decode_tokens,
            )
        )

    # 3. Prefix cache. Same long prompt twice in a row.
    shared = filler(rng, max(prompt_chars)) + "\n\nAnswer with the single word: ok."
    result.samples.append(_measure(client, "cache/cold", shared, max_tokens=8))
    result.samples.append(_measure(client, "cache/warm", shared, max_tokens=8))

    # 4. What a compaction costs: the same prompt with the middle rewritten.
    mutated = shared[: len(shared) // 2] + " REWRITTEN " + shared[len(shared) // 2 :]
    result.samples.append(_measure(client, "cache/mutated", mutated, max_tokens=8))

    return result


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def summarise(result: BenchResult) -> dict[str, Any]:
    prefill_rows = []
    for sample in result.by_label("prefill/"):
        prefill_rows.append(sample)

    by_size: dict[int, list[Sample]] = {}
    for sample in prefill_rows:
        by_size.setdefault(sample.prompt_tokens // 256 * 256, []).append(sample)

    prefill_summary = []
    for _, group in sorted(by_size.items()):
        prefill_summary.append(
            {
                "prompt_tokens": int(_median([float(s.prompt_tokens) for s in group]) or 0),
                "median_ttft_s": _median([s.ttft_s for s in group if s.ttft_s is not None]),
                "median_prefill_tok_s": _median(
                    [s.prefill_tok_s for s in group if s.prefill_tok_s is not None]
                ),
                "n": len(group),
            }
        )

    decode = result.by_label("decode")
    cold = next((s for s in result.samples if s.label == "cache/cold"), None)
    warm = next((s for s in result.samples if s.label == "cache/warm"), None)
    mutated = next((s for s in result.samples if s.label == "cache/mutated"), None)

    cache: dict[str, Any] = {}
    if cold and warm and cold.ttft_s and warm.ttft_s:
        cache = {
            "cold_ttft_s": round(cold.ttft_s, 3),
            "warm_ttft_s": round(warm.ttft_s, 3),
            "speedup": round(cold.ttft_s / warm.ttft_s, 2) if warm.ttft_s else None,
            "warm_cached_tokens": warm.cached_tokens,
            "mutated_ttft_s": round(mutated.ttft_s, 3) if mutated and mutated.ttft_s else None,
        }

    decode_rate = _median([s.decode_tok_s for s in decode if s.decode_tok_s is not None])

    # Project an agent turn. These shapes come from the real orchestrator: the
    # system prompt plus a skill body is about 1500 tokens, each tool result
    # adds a few hundred, a tool call is short.
    projection = None
    if prefill_summary and decode_rate:
        biggest = prefill_summary[-1]
        rate = biggest["median_prefill_tok_s"]
        if rate:
            cold_turn = 4000 / rate + 80 / decode_rate
            warm_turn = 600 / rate + 80 / decode_rate
            projection = {
                "assumed_prompt_tokens": 4000,
                "assumed_new_tokens_per_turn": 600,
                "assumed_completion_tokens": 80,
                "turn_s_no_cache": round(cold_turn, 1),
                "turn_s_with_prefix_cache": round(warm_turn, 1),
                "task_s_15_turns_no_cache": round(cold_turn * 15, 0),
                "task_s_15_turns_with_cache": round(warm_turn * 15, 0),
            }

    return {
        "endpoint": result.endpoint,
        "model": result.model,
        "device": result.device,
        "host": result.host,
        "prefill": prefill_summary,
        "median_decode_tok_s": decode_rate,
        "cache": cache,
        "projection": projection,
        "notes": result.notes,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Benchmark: {summary['model']} on {summary['device']}",
        "",
        f"- endpoint: `{summary['endpoint']}`",
        f"- host: {summary['host'].get('platform', 'unknown')}",
        f"- cpu: {summary['host'].get('processor', 'unknown')}, "
        f"{summary['host'].get('cpu_count', '?')} logical cores, "
        f"{summary['host'].get('ram_gb', '?')} GB RAM",
        "",
        "## Prefill",
        "",
        "| prompt tokens | median TTFT (s) | prefill tok/s | n |",
        "|---:|---:|---:|---:|",
    ]
    for row in summary["prefill"]:
        lines.append(
            f"| {row['prompt_tokens']} | {row['median_ttft_s']} | "
            f"{row['median_prefill_tok_s']} | {row['n']} |"
        )

    lines += ["", "## Decode", "", f"median sustained decode: **{summary['median_decode_tok_s']} tok/s**", ""]

    cache = summary.get("cache") or {}
    if cache:
        lines += [
            "## Prefix cache",
            "",
            f"- cold TTFT: {cache.get('cold_ttft_s')} s",
            f"- warm TTFT (identical prompt): {cache.get('warm_ttft_s')} s",
            f"- speedup: **{cache.get('speedup')}x**, "
            f"{cache.get('warm_cached_tokens')} tokens reported cached",
            f"- same prompt with the middle rewritten: {cache.get('mutated_ttft_s')} s "
            "(this is what one context compaction costs you)",
            "",
        ]

    projection = summary.get("projection")
    if projection:
        lines += [
            "## Projected agent cost",
            "",
            f"Assuming a {projection['assumed_prompt_tokens']} token prompt growing by "
            f"{projection['assumed_new_tokens_per_turn']} tokens per turn and "
            f"{projection['assumed_completion_tokens']} tokens out:",
            "",
            f"- per turn without a prefix cache: **{projection['turn_s_no_cache']} s**",
            f"- per turn with a prefix cache: **{projection['turn_s_with_prefix_cache']} s**",
            f"- 15 turn task, no cache: {projection['task_s_15_turns_no_cache']:.0f} s",
            f"- 15 turn task, cached: {projection['task_s_15_turns_with_cache']:.0f} s",
            "",
        ]

    for note in summary.get("notes", []):
        lines.append(f"> {note}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument(
        "--prompt-chars",
        default="1000,4000,8000,16000,32000",
        help="comma separated approximate prompt sizes in characters",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--decode-tokens", type=int, default=192)
    parser.add_argument("--out", default="bench.json")
    parser.add_argument("--markdown", default=None, help="also write a markdown report")
    args = parser.parse_args()

    cfg = MODEL_PRESETS.get(args.profile, ModelConfig()) if args.profile else ModelConfig.from_env()
    if args.base_url:
        cfg = ModelConfig(**{**cfg.__dict__, "base_url": args.base_url})
    if args.model:
        cfg = ModelConfig(**{**cfg.__dict__, "model": args.model})
    cfg = ModelConfig(**{**cfg.__dict__, "stream": True})

    client = OpenAICompatibleClient(cfg)
    try:
        probe = client.probe()
    except Exception as exc:
        print(f"server unreachable at {cfg.base_url}: {type(exc).__name__}: {exc}")
        return 1
    if not probe["configured_model_present"]:
        print(f"warning: {cfg.model} is not in {probe['served_models']}, continuing anyway")

    sizes = [int(x) for x in args.prompt_chars.split(",") if x.strip()]
    print(f"benchmarking {cfg.model} on {cfg.device_note} via {cfg.base_url}")
    print(f"prompt sizes (chars): {sizes}, repeats: {args.repeats}\n")

    result = run_benchmark(
        client,
        endpoint=cfg.base_url,
        model=cfg.model,
        device=cfg.device_note,
        prompt_chars=sizes,
        repeats=args.repeats,
        decode_tokens=args.decode_tokens,
    )

    summary = summarise(result)
    Path(args.out).write_text(
        json.dumps({"summary": summary, "samples": [s.as_dict() for s in result.samples]},
                   indent=2),
        encoding="utf-8",
    )
    report = render_markdown(summary)
    print(report)
    if args.markdown:
        Path(args.markdown).write_text(report, encoding="utf-8")
        print(f"markdown: {args.markdown}")
    print(f"raw: {args.out}")

    if summary["prefill"] and not summary["prefill"][0]["median_ttft_s"]:
        print(
            "\nNOTE: no time-to-first-token was recorded, so the server did not "
            "stream. Prefill and decode cannot be separated without it."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
