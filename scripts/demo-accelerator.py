#!/usr/bin/env python3
"""Human-facing accelerator smoke demo.

Runs the same locally cached Qwen3-8B OpenVINO model on one explicitly selected
accelerator and keeps inference busy long enough to watch the matching graph in
Task Manager, HWiNFO, intel_gpu_top, or another OS monitor.

This is a demo/smoke path, not an experiment runner and not scored evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent.config import MODEL_PRESETS  # noqa: E402
from local_agent.llm.client import OpenAICompatibleClient  # noqa: E402
from measurement import serve  # noqa: E402

BASE_PROFILE = "ptl-npu-8b"
DEMO_PROFILE = BASE_PROFILE
DEVICES = ("CPU", "GPU", "NPU")
WIDTH = 68
PROMPT = (
    "Produce a compact C++ code review checklist with exactly 20 numbered items. "
    "Do not use tools. Do not explain your reasoning."
)

LOGO = (
    " _      ____    _    ",
    "| |    / ___|  / \\   ",
    "| |   | |     / _ \\  ",
    "| |___| |___ / ___ \\ ",
    "|_____\\____/_/   \\_\\",
)


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
        device_note=f"{device} accelerator demo",
        # Demo latency should not be dominated by hidden Qwen reasoning. The
        # qualification path separately verifies whether the server honours it.
        thinking=False,
        stream=True,
    )


def monitor_hint(device: str) -> str:
    if os.name == "nt":
        return f"Task Manager -> Performance -> {device}"
    if device == "GPU":
        return "your Linux GPU monitor (for Intel, intel_gpu_top if installed)"
    if device == "NPU":
        return "your Linux NPU/OpenVINO telemetry tool"
    return "top/htop or your CPU monitor"


def section(title: str) -> None:
    label = f" {title} "
    remaining = max(1, WIDTH - len(label))
    print(f"{label}{'-' * remaining}")


def print_banner() -> None:
    print()
    for line in LOGO:
        print(f"  {line}")
    print()
    print("  LOCAL CODE AGENT")
    print("  Local Model Accelerator Demo")
    print("=" * WIDTH)


def stop_owned_previous(plan) -> None:
    """Stop only a process the deterministic serving controller can prove it owns."""
    record = serve.read_record(plan)
    if not record:
        return
    state = serve.status(plan)
    if state["process_alive"]:
        print(f"  Stopping previously started demo server (PID {state['pid']})")
        serve.stop(plan)
    else:
        # Clear stale owned state through the same controller path.
        serve.stop(plan)


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

    print_banner()
    section("MODEL SETUP")
    print(f"  Model            {cfg.model.split('/')[-1]}")
    print("  Backend          OVMS / OpenVINO")
    print(f"  Requested device {cfg.device}")
    print(f"  Hardware view    {monitor_hint(cfg.device)}")
    print()

    stop_owned_previous(plan)
    section("STARTUP")
    print("  [1/3] Starting the model server through the validated serving path...")
    state = serve.start(plan, cfg, wait_seconds=900)
    print(f"        Ready at          {cfg.base_url}")
    print(f"        Process ID        {state['pid']}")
    print(f"        Device confirmed  {state['resolved_device']}")
    print()

    section("INFERENCE RUN")
    print(f"  [2/3] Running repeated model inference for {seconds:.0f} seconds")
    print(f"        Watch now: {monitor_hint(cfg.device)}")

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

                print()
                if calls == 1:
                    print("  Request 1 | COLD START")
                    note = "One-time runtime warm-up + prompt processing"
                else:
                    print(f"  Request {calls} | WARM")
                    note = "Runtime already initialised; prompt processing still happens"
                print(f"    First token       {ttft}")
                print(f"    Generation speed  {rate}")
                print(f"    Output length     {length}")
                print(f"    Note              {note}")
            except Exception as exc:  # keep the demo visibly diagnostic
                failures += 1
                print()
                print(f"  Request {calls} | FAILED")
                print(f"    {type(exc).__name__}: {exc}")
                break
    finally:
        print()
        section("SERVER")
        if keep_server:
            print(f"  Server left running at {cfg.base_url}")
        else:
            serve.stop(plan)
            print("  Model server stopped cleanly")

    if calls == 0 or failures:
        return 1

    print()
    section("SUMMARY")
    print("  [3/3] RESULT         PASS")
    print(f"        Device         {cfg.device}")
    print(f"        Requests       {calls} completed successfully")
    if rates:
        print(f"        Average speed  {sum(rates) / len(rates):.1f} tokens/s")
    if ttfts:
        print(f"        Best first token {min(ttfts):.2f} s")
    print()
    print("  Timing values are live demo observations, not benchmark results.")
    print("=" * WIDTH)
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device",
        default="NPU",
        type=str.upper,
        choices=(*DEVICES, "ALL"),
        help="accelerator to demonstrate; ALL runs CPU, GPU, then NPU sequentially",
    )
    parser.add_argument("--seconds", type=float, default=45.0,
                        help="repeated inference time per device (default: 45)")
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--executable", help="override the OVMS executable")
    parser.add_argument("--model-dir", type=Path,
                        help="override the already-downloaded OpenVINO model directory")
    parser.add_argument("--keep-server", action="store_true",
                        help="leave the final server running after the load ends")
    args = parser.parse_args(argv)

    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.device == "ALL" and args.keep_server:
        parser.error("--keep-server is only meaningful for one selected device")

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
        print(f"Demo refused to run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
