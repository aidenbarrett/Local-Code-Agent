#!/usr/bin/env python3
"""Human-facing local AI hardware demo.

Runs the same locally cached Qwen3-8B OpenVINO model on one explicitly selected
CPU, GPU or NPU target and keeps inference busy long enough to watch the matching
hardware activity in the operating-system monitor.

This is a demo/smoke path, not an experiment runner and not scored evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import time

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from local_agent.config import MODEL_PRESETS  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402
from measurement import serve  # noqa: E402
from terminal_ui import LCA_LOGO, WIDTH, device_label, ui  # noqa: E402

BASE_PROFILE = "ptl-npu-8b"
DEMO_PROFILE = BASE_PROFILE
DEVICES = ("CPU", "GPU", "NPU")
PROMPT = (
    "Produce a compact C++ code review checklist with exactly 20 numbered items. "
    "Do not use tools. Do not explain your reasoning."
)

# Compatibility alias for existing tests and any external presentation checks.
LOGO = LCA_LOGO


def default_runtime_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalCodeAgent"


def demo_config(device: str):
    """Use one model/artifact and change only the requested execution device."""
    device = device.upper()
    if device not in DEVICES:
        raise ValueError(f"device must be one of {', '.join(DEVICES)}")
    base = MODEL_PRESETS[BASE_PROFILE]
    return replace(
        base,
        device=device,
        device_note=f"{device} local AI hardware demo",
        thinking=False,
        stream=True,
    )


def monitor_hint(device: str) -> str:
    if os.name == "nt":
        return f"Task Manager > Performance > {device}"
    if device == "GPU":
        return "your Linux GPU monitor (for Intel, intel_gpu_top if installed)"
    if device == "NPU":
        return "your Linux NPU / OpenVINO telemetry tool"
    return "top / htop or your CPU monitor"


def stop_owned_previous(plan):
    """Stop only a process the deterministic serving controller can prove it owns."""
    record = serve.read_record(plan)
    if not record:
        return None
    state = serve.status(plan)
    pid = state["pid"]
    serve.stop(plan)
    return pid


def run_device(device: str, *, seconds: float, runtime_root: Path,
               keep_server: bool, executable: str | None, model_dir: Path | None) -> int:
    cfg = demo_config(device)
    plan = serve.make_plan(
        DEMO_PROFILE,
        cfg,
        runtime_root,
        executable=executable,
        model_dir=model_dir,
    )
    term = ui()

    term.banner(
        "LOCAL AI HARDWARE DEMO",
        "Same local Qwen3-8B model. Only the hardware target changes.",
    )

    term.section("MODEL & HARDWARE")
    term.field("Model", "Qwen3-8B (INT4)")
    term.field("Running on", device_label(cfg.device), role="cyan")
    term.field("Backend", "OpenVINO Model Server")
    term.field("Watch live", monitor_hint(cfg.device))

    stopped_pid = stop_owned_previous(plan)

    term.line()
    term.section("STARTUP")
    if stopped_pid:
        term.status("info", "Previous Local Code Agent model server stopped cleanly")
        term.line()
    term.status("active", "[1/3] Starting the local model server")
    state = serve.start(plan, cfg, wait_seconds=900)
    term.status("ok", "Model server ready")
    term.status("ok", f"Hardware target confirmed: {state['resolved_device']}")

    term.line()
    term.section("LIVE INFERENCE")
    term.status("active", f"[2/3] Generating repeated responses for {seconds:.0f} seconds")
    term.field("Watch hardware", monitor_hint(cfg.device))

    client = OpenAICompatibleClient(cfg)
    deadline = time.monotonic() + seconds
    calls = 0
    failures = 0
    ttfts: list[float] = []
    rates: list[float] = []
    try:
        while time.monotonic() < deadline:
            calls += 1
            try:
                reply = client.chat(
                    [{"role": "user", "content": PROMPT}],
                    tools=None,
                    max_tokens=256,
                )
                stats = reply.stats
                if stats.ttft_s is not None:
                    ttfts.append(stats.ttft_s)
                if stats.decode_tok_s is not None:
                    rates.append(stats.decode_tok_s)
                ttft = f"{stats.ttft_s:.2f} s" if stats.ttft_s is not None else "n/a"
                rate = (f"{stats.decode_tok_s:.1f} tokens/s"
                        if stats.decode_tok_s is not None else "n/a")
                length = (f"{stats.completion_tokens} tokens"
                          if stats.completion_tokens is not None else "n/a")

                term.line()
                if calls == 1:
                    term.request_header(calls, "COLD START")
                    note = "One-time runtime warm-up + prompt processing"
                else:
                    term.request_header(calls, "WARM")
                    note = "Runtime already initialised; prompt processing still happens"
                term.field("First token", ttft)
                term.field("Generation speed", rate)
                term.field("Output length", length)
                term.field("Note", note)
            except Exception as exc:
                failures += 1
                term.line()
                term.request_header(calls, "FAILED")
                term.status("fail", f"{type(exc).__name__}: {exc}")
                break
    finally:
        term.line()
        term.section("MODEL SERVER")
        if keep_server:
            term.status("active", "Model server left running for local chat")
        else:
            serve.stop(plan)
            term.status("ok", "Model server stopped cleanly")

    if calls == 0 or failures:
        return 1

    term.line()
    term.section("RESULT")
    term.status("ok", "[3/3] DEMO COMPLETE")
    term.field("Hardware target", device_label(cfg.device))
    term.field("Responses", f"{calls} completed successfully")
    if rates:
        term.field("Average speed", f"{sum(rates) / len(rates):.1f} tokens/s")
    if ttfts:
        term.field("Best first token", f"{min(ttfts):.2f} s")
    term.footer_note("Live demo observations · not benchmark results.")
    term.rule("═", role="cyan")
    term.line()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        default="NPU",
        type=str.upper,
        choices=(*DEVICES, "ALL"),
        help="hardware target to demonstrate; ALL runs CPU, GPU, then NPU sequentially",
    )
    parser.add_argument("--seconds", type=float, default=45.0,
                        help="repeated inference time per hardware target (default: 45)")
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--executable", help="override the OpenVINO Model Server executable")
    parser.add_argument("--model-dir", type=Path,
                        help="override the already-downloaded OpenVINO model directory")
    parser.add_argument("--keep-server", action="store_true",
                        help="leave the final model server running after the demo")
    args = parser.parse_args(argv)

    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.device == "ALL" and args.keep_server:
        parser.error("--keep-server is only meaningful for one selected hardware target")

    devices = DEVICES if args.device == "ALL" else (args.device,)
    try:
        for device in devices:
            rc = run_device(
                device,
                seconds=args.seconds,
                runtime_root=args.runtime_root,
                keep_server=args.keep_server,
                executable=args.executable,
                model_dir=args.model_dir,
            )
            if rc:
                return rc
        return 0
    except (serve.Refusal, OSError, ValueError) as exc:
        print(f"Demo could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
