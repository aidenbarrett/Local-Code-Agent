#!/usr/bin/env python3
"""One command, one artefact: the whole benchmark for one configuration.

    python measurement/run_benchmark_suite.py --profile nuc-cpu-30b \
        --label "Qwen3-Coder 30B INT4, CPU, DDR4-3200 NUC12" \
        --outdir results/nuc-ddr4

Runs, in order:

  1. rehearsal        the harness with no model, to prove nothing local is broken
  2. characterisation prefill sweep, decode rate, prompt cache speedup
  3. behaviour        the nine eval cases against the real repository
  4. report           one markdown file with all of it and a verdict

Host CPU, memory and, where the counters are readable, package energy are
sampled throughout, so the report can quote joules per completed task. On a
comparison between a CPU and an NPU that is the column that actually settles
the argument.

Everything is written under `--outdir` so a run is one directory you can attach
to a mail, and so two runs can never quietly overwrite each other.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "measurement"))
sys.path.insert(0, str(REPO / "evaluation"))

from benchmark_model import render_markdown, run_benchmark, summarise  # noqa: E402
from machine_telemetry import HostMonitor  # noqa: E402

from local_agent.config import MODEL_PRESETS, ModelConfig  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402


class OfflineModel:
    """Stand-in used by --rehearse-all so the whole artefact chain can be
    produced with no model server at all. Its numbers are synthetic and the
    report says so; it exists to prove the pipeline, not the hardware."""

    def __init__(self, prefill_rate: float = 300.0, decode_rate: float = 11.0) -> None:
        self.prefill_rate = prefill_rate
        self.decode_rate = decode_rate
        self._seen: set[str] = set()

    def chat(self, messages, tools=None, max_tokens=None):
        from local_agent.llm.models import CallStats, ChatResponse

        text = "".join(str(m.get("content") or "") for m in messages)
        prompt_tokens = max(1, len(text) // 4)
        cached = prompt_tokens if text in self._seen else 0
        self._seen.add(text)
        ttft = (prompt_tokens - cached) / self.prefill_rate + 0.05
        completion = min(max_tokens or 32, 32)
        time.sleep(min(0.02, ttft / 50))  # keep the telemetry sampler honest
        return ChatResponse(
            content="ok " * completion,
            stats=CallStats(
                total_s=ttft + (completion - 1) / self.decode_rate,
                ttft_s=ttft,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion,
                cached_tokens=cached,
                streamed=True,
            ),
        )


def _run_evaluation(
    outdir: Path, profile: str | None, base_url: str, model: str, label: str,
    repeat: int, rehearse: bool, cheap_profile: str | None = None,
) -> dict[str, Any]:
    out = outdir / ("rehearsal.json" if rehearse else "evals.json")
    cmd = [
        sys.executable, str(REPO / "evaluation" / "run_evaluation.py"),
        "--out", str(out),
        "--repeat", str(repeat),
        "--workdir", str(outdir / "work"),
        "--label", label,
    ]
    if cheap_profile and not rehearse:
        cmd += ["--cheap-profile", cheap_profile]
    if rehearse:
        cmd.append("--rehearse")
    elif profile:
        cmd += ["--profile", profile]
    else:
        cmd += ["--base-url", base_url, "--model", model]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    (outdir / ("rehearsal.log" if rehearse else "evals.log")).write_text(
        proc.stdout + proc.stderr, encoding="utf-8"
    )
    if proc.returncode != 0 or not out.is_file():
        return {"failed": True, "log": proc.stdout[-4000:] + proc.stderr[-4000:]}
    return json.loads(out.read_text(encoding="utf-8"))


def _pct(value: Any) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


def _num(value: Any, places: int) -> str:
    """Format a measurement, or say plainly that there was not one."""
    if value is None:
        return "not measured"
    return f"{value:.{places}f}"


def render_suite(payload: dict[str, Any]) -> str:
    bench = payload["benchmark"]
    evals = payload["evals"]
    telem = payload["telemetry"]
    cfg = payload["config"]

    lines = [
        f"# Benchmark: {cfg['label']}",
        "",
        f"- model: `{cfg['model']}`",
        f"- device: **{cfg['device']}**, endpoint `{cfg['base_url']}`",
        f"- context budget: {cfg['context_budget_tokens']} tokens",
        f"- host: {cfg['host']['platform']}",
        f"- cpu: {cfg['host'].get('processor')}, "
        f"{cfg['host'].get('cpu_count')} logical cores, "
        f"{cfg['host'].get('ram_gb')} GB RAM",
        f"- memory type: {cfg.get('memory_note') or 'not stated (pass --memory-note)'}",
        f"- run at: {cfg['started_at']}",
        "- configuration identity: "
        + ", ".join(f"{k}={v}" for k, v in (cfg.get("identity") or {}).items()),
        "",
        "## Headline",
        "",
    ]

    prefill = bench.get("prefill") or []
    decode = bench.get("median_decode_tok_s")
    cache = bench.get("cache") or {}
    diag = evals.get("diagnostic_score")
    overall = evals.get("overall")

    biggest = prefill[-1] if prefill else {}
    lines += [
        f"| metric | value |",
        f"|---|---:|",
        f"| prefill rate at {biggest.get('prompt_tokens', '?')} tokens | "
        f"{_num(biggest.get('median_prefill_tok_s'), 0)} tok/s |",
        f"| time to first token at that length | "
        f"{_num(biggest.get('median_ttft_s'), 2)} s |",
        f"| sustained decode | {_num(decode, 1)} tok/s |",
        f"| prompt cache speedup | {_num(cache.get('speedup'), 1)}x |",
        f"| eval score, overall weighted | {_num(overall, 3)} |",
        f"| eval score, diagnostic subset | {_num(diag, 3)} |",
        f"| median task wall clock | {payload['task_seconds']['median']} s |",
        f"| p90 task wall clock | {payload['task_seconds']['p90']} s |",
    ]
    if telem.get("package_energy_j"):
        lines.append(
            f"| whole-platform energy for the eval pass | "
            f"{telem['package_energy_j']} J at {telem['average_watts']} W mean |"
        )
    if payload.get("joules_per_task"):
        lines.append(
            f"| whole-platform energy per completed task | "
            f"{payload['joules_per_task']} J |"
        )
    if telem.get("peak_cpu_percent") is not None:
        lines.append(f"| peak host CPU during the pass | {telem['peak_cpu_percent']} % |")
    if telem.get("peak_mem_used_gb") is not None:
        lines.append(f"| peak host memory in use | {telem['peak_mem_used_gb']} GB |")
    ledger = evals.get("ledger") or {}
    if ledger:
        lines.append(
            f"| tasks the cheap tier finished alone | "
            f"{ledger['cheap_only_success']} of {ledger['attempted']} "
            f"({_pct(ledger['cheap_alone_rate'])}) |"
        )
        lines.append(
            f"| tasks completed locally at either tier | "
            f"{ledger['local_success']} of {ledger['attempted']} "
            f"({_pct(ledger['local_success_rate'])}) |"
        )
        lines.append(
            f"| would still need the cloud | {_pct(ledger['cloud_required_rate'])} |"
        )
        lines.append(
            f"| model capability (blocked excluded) | "
            f"{_pct(ledger.get('model_capability_rate'))} |"
        )
        lines.append(
            f"| end-to-end completion (blocked included) | "
            f"{_pct(ledger.get('end_to_end_rate'))} |"
        )
        if ledger["blocked"]:
            lines.append(
                f"| blocked by the environment, not the model | {ledger['blocked']} |"
            )

    lines += ["", "## Verdict", "", payload["verdict"], ""]

    if ledger and evals.get("cheap_profile"):
        lines += [
            "## Two-tier routing",
            "",
            f"Cheap tier: `{evals.get('cheap_profile')}`. Every skill starts on it "
            "and escalates to the strong tier only when it halts, says nothing, "
            "claims success it has not verified, or cannot produce the tool-backed "
            "result its skill requires. Nothing is hard-wired to the strong tier, "
            "so this measures the split rather than assuming it.",
            "",
            "```",
            f"Tasks                   {ledger['tasks']}",
            f"Blocked (environment)   {ledger['blocked']}",
            f"Attempted               {ledger['attempted']}",
            "",
            f"Cheap tier alone        {ledger['cheap_only_success']}/{ledger['attempted']}"
            f"   {_pct(ledger['cheap_alone_rate'])}",
            f"Escalated success       {ledger['escalated_success']}/{ledger['escalated_tasks']}",
            f"Local success total     {ledger['local_success']}/{ledger['attempted']}"
            f"   {_pct(ledger['local_success_rate'])}",
            f"Cloud would be needed   {_pct(ledger['cloud_required_rate'])}",
            "",
            f"Model calls             cheap {ledger['calls_by_tier']['cheap']}, "
            f"strong {ledger['calls_by_tier']['strong']}",
            f"Median task             cheap-only {ledger['median_seconds_cheap_only']}s, "
            f"escalated {ledger['median_seconds_escalated']}s",
            "```",
            "",
            "Counted per task, not per model call. Six internal turns to answer "
            "one git query is one task, not six wins.",
            "",
            "Energy above is whole-platform, measured across the pass. In a "
            "two-tier run it cannot be attributed to the NPU or the iGPU "
            "separately: for that, compare the single-tier runs. Nothing here "
            "is labelled 'NPU joules', because no counter measured that.",
            "",
        ]
        if ledger["escalations"]:
            lines += ["Escalated:", ""]
            lines += [f"- `{e['case']}`: {e['reason']}" for e in ledger["escalations"]]
            lines += [""]
        if ledger["blocked_cases"]:
            lines += [
                "Blocked by the environment (not counted against either tier): "
                + ", ".join(f"`{c}`" for c in ledger["blocked_cases"]),
                "",
            ]

    lines += ["## Hardware characterisation", ""]
    bench_md = render_markdown(bench)
    parts = bench_md.split("## Prefill", 1)
    lines.append("## Prefill" + parts[1] if len(parts) > 1 else bench_md)

    lines += ["## Behaviour, case by case", "", "| case | score | tool calls | seconds |",
              "|---|---:|---:|---:|"]
    for row in evals.get("rows", []):
        lines.append(
            f"| {row['case']}{' *' if row['case'] in payload['diagnostic_cases'] else ''} "
            f"| {row['score']} | {row.get('tool_calls', '-')} | {row.get('elapsed_s')} |"
        )
    lines += ["", "`*` diagnostic case: counts toward the kill threshold.", ""]

    misses: list[str] = []
    for row in evals.get("rows", []):
        for check, ok in (row.get("checks") or {}).items():
            if not ok:
                misses.append(f"- `{row['case']}`: missed **{check}**")
    if misses:
        lines += ["## What it got wrong", "", *misses[:40], ""]
        lines += [
            "Read these before touching anything. A miss on 'verified before "
            "claiming success' or 'was efficient' is an orchestrator problem and "
            "you can fix it in code. A miss on 'named the right file' is model "
            "capacity and no prompt work will rescue it.",
            "",
        ]

    for note in telem.get("notes", []):
        lines.append(f"> {note}")
    lines += ["", "Method: see `docs/measurement-protocol.md`.", ""]
    return "\n".join(lines)


def build_verdict(evals: dict[str, Any], bench: dict[str, Any],
                  kill_threshold: float, interactive_seconds: float) -> str:
    diag = evals.get("diagnostic_score")
    prefill = bench.get("prefill") or []
    ttft = prefill[-1].get("median_ttft_s") if prefill else None

    if diag is None:
        return "No diagnostic cases ran, so there is no verdict to give."
    if diag < kill_threshold:
        return (
            f"**Not a daily driver.** Diagnostic subset scored {diag}, below the "
            f"{kill_threshold} threshold fixed before this data was collected. "
            "The failures are in reaching the right conclusion, not in speed, so "
            "prompt engineering will not rescue it. Keep the configuration for "
            "batch triage if the misses are narrow, otherwise bin it."
        )
    if ttft is not None and ttft > interactive_seconds:
        return (
            f"**Batch tool, not an interactive one.** Diagnostic subset scored "
            f"{diag}, which is good enough to trust, but {ttft} s to first token "
            "at full context means nobody will sit and watch it. Point it at "
            "overnight build triage, pre-standup diff review and test failure "
            "classification, where latency costs nothing."
        )
    return (
        f"**Usable in the loop.** Diagnostic subset scored {diag} and time to "
        f"first token is {ttft} s at full context, so it is both accurate enough "
        "to trust and quick enough to wait for."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(MODEL_PRESETS))
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--label", help="how this configuration should be named in the report")
    parser.add_argument("--memory-note", help='e.g. "DDR4-3200 dual channel, 64 GB"')
    parser.add_argument(
        "--cheap-profile",
        choices=sorted(MODEL_PRESETS),
        help="measure a two-tier deployment: cheap-tier skills go here and "
             "escalate to --profile only on failure",
    )
    parser.add_argument("--outdir", default="results/run")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--bench-repeats", type=int, default=5)
    parser.add_argument("--prompt-chars", default="1000,4000,8000,16000,32000")
    parser.add_argument("--skip-rehearsal", action="store_true")
    parser.add_argument(
        "--rehearse-all",
        action="store_true",
        help="produce the whole report with no model server, to validate the "
             "artefact chain before you have one",
    )
    parser.add_argument("--sample-interval", type=float, default=1.0)
    args = parser.parse_args()

    from compare_datasets import INTERACTIVE_TURN_SECONDS
    from task_contracts import DIAGNOSTIC_CASES, KILL_THRESHOLD

    cfg = MODEL_PRESETS.get(args.profile, ModelConfig()) if args.profile else ModelConfig.from_env()
    if args.base_url:
        cfg = ModelConfig(**{**cfg.__dict__, "base_url": args.base_url})
    if args.model:
        cfg = ModelConfig(**{**cfg.__dict__, "model": args.model})
    cfg = ModelConfig(**{**cfg.__dict__, "stream": True})
    label = args.label or f"{cfg.model} on {cfg.device_note}"

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S %z")

    print(f"== {label}")
    print(f"   endpoint {cfg.base_url}, writing to {outdir}\n")

    if not args.skip_rehearsal:
        print("[1/4] rehearsal with no model")
        rehearsal = _run_evaluation(outdir, None, "", "", "REHEARSAL", 1, rehearse=True)
        if rehearsal.get("failed"):
            print("      rehearsal FAILED, so the harness is broken, not the model:")
            print(rehearsal["log"][-2000:])
            return 2
        print("      harness, scoring and reporting all work\n")

    if args.rehearse_all:
        client: Any = OfflineModel()
        label = "REHEARSAL, no model involved"
        cfg = ModelConfig(**{**cfg.__dict__, "model": "none (rehearsal)",
                             "device_note": "none (rehearsal)",
                             "base_url": "none (rehearsal)"})
        print("      running the whole suite offline; every number below is "
              "synthetic\n")
    else:
        client = OpenAICompatibleClient(cfg)
        try:
            probe = client.probe()
        except Exception as exc:
            print(f"model server unreachable at {cfg.base_url}: {type(exc).__name__}: {exc}")
            return 1
        if not probe["configured_model_present"]:
            print(f"warning: {cfg.model} not in {probe['served_models']}, continuing\n")

    print("[2/4] hardware characterisation")
    sizes = [int(x) for x in args.prompt_chars.split(",") if x.strip()]
    with HostMonitor(args.sample_interval) as bench_monitor:
        bench_raw = run_benchmark(
            client,
            endpoint=cfg.base_url,
            model=cfg.model,
            device=cfg.device_note,
            prompt_chars=sizes,
            repeats=args.bench_repeats,
            decode_tokens=192,
        )
    bench = summarise(bench_raw)
    (outdir / "bench.json").write_text(
        json.dumps({"summary": bench,
                    "samples": [s.as_dict() for s in bench_raw.samples],
                    "telemetry": bench_monitor.report.as_dict()}, indent=2),
        encoding="utf-8",
    )
    print(f"      decode {bench.get('median_decode_tok_s')} tok/s, "
          f"cache speedup {(bench.get('cache') or {}).get('speedup')}x\n")

    print("[3/4] behaviour on the eval cases")
    with HostMonitor(args.sample_interval) as eval_monitor:
        evals = _run_evaluation(
            outdir, args.profile, cfg.base_url, cfg.model, label, args.repeat,
            rehearse=args.rehearse_all, cheap_profile=args.cheap_profile,
        )
    if evals.get("failed"):
        print("      eval pass FAILED:")
        print(evals["log"][-2000:])
        return 2

    telemetry = eval_monitor.report.as_dict()
    rows = evals.get("rows", [])
    seconds = sorted(r["elapsed_s"] for r in rows if r.get("elapsed_s"))
    task_seconds = {
        "median": round(seconds[len(seconds) // 2], 1) if seconds else None,
        "p90": round(seconds[int(len(seconds) * 0.9) - 1], 1) if len(seconds) >= 2 else None,
    }
    completed = [r for r in rows if r.get("score", 0) > 0]
    joules_per_task = None
    if telemetry.get("package_energy_j") and completed:
        joules_per_task = round(telemetry["package_energy_j"] / len(completed), 1)

    print("[4/4] report")
    payload = {
        "config": {
            "label": label,
            "model": cfg.model,
            "device": cfg.device_note,
            "base_url": cfg.base_url,
            "context_budget_tokens": cfg.context_budget_tokens,
            "identity": cfg.identity(),
            "memory_note": args.memory_note,
            "started_at": started_at,
            "host": {
                "platform": platform.platform(),
                "processor": platform.processor() or platform.machine(),
                **{k: v for k, v in bench.get("host", {}).items() if k != "platform"},
            },
        },
        "benchmark": bench,
        "evals": evals,
        "telemetry": telemetry,
        "task_seconds": task_seconds,
        "joules_per_task": joules_per_task,
        "diagnostic_cases": sorted(DIAGNOSTIC_CASES),
        "verdict": build_verdict(evals, bench, KILL_THRESHOLD, INTERACTIVE_TURN_SECONDS),
    }

    if args.rehearse_all:
        payload["verdict"] = (
            "**REHEARSAL. No model was involved and every number above is "
            "synthetic.** This run exists to prove the benchmark, scoring, "
            "telemetry and reporting all work end to end. Do not quote it."
        )

    (outdir / "suite.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    report = render_suite(payload)
    (outdir / "report.md").write_text(report, encoding="utf-8")

    print()
    print(report)
    produced = ", ".join(sorted(p.name for p in outdir.glob("*.json")))
    print(f"\nwritten: {outdir}/report.md ({produced})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
