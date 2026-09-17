#!/usr/bin/env python3
"""Talk directly to a local model. No agent, tools, or repository access.

Use the root PowerShell entrypoint on Windows:

    .\chat.ps1
    .\chat.ps1 qwen3-8b-npu

This is the simplest user-facing path through the project: choose a configured
model/device profile and have a normal terminal conversation. Local Code Agent
is separate; it adds controlled repository access, restricted tools, skills and
independent verification.

The chat path uses the same client, model profiles and serving endpoints as the
rest of the project. There is no demo-only model path.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from local_agent.config import MODEL_PRESETS, ModelConfig  # noqa: E402

# Friendly names are a user-facing layer over the existing preregistered model
# profiles. Qwen3-8B has one profile; CPU/GPU/NPU are explicit device overrides,
# matching the accelerator demo rather than inventing new behavioural profiles.
FRIENDLY: dict[str, tuple[str, str]] = {
    "qwen3-8b-npu": ("ptl-npu-8b", "NPU"),
    "qwen3-8b-gpu": ("ptl-npu-8b", "GPU"),
    "qwen3-8b-cpu": ("ptl-npu-8b", "CPU"),
    "qwen3-coder-30b": ("ptl-gpu-30b", "GPU"),
}

BANNER = "Local Model Chat"
RUNTIME_LABEL = {"ovms": "OVMS / OpenVINO", "llamacpp": "llama.cpp", "cloud": "Cloud"}


def _resolve(name: str) -> tuple[str, str, ModelConfig] | None:
    """Return (profile, device, config), or None if the name is unknown."""
    if name in FRIENDLY:
        profile, device = FRIENDLY[name]
    else:
        profile, device = name, ""
    base = MODEL_PRESETS.get(profile)
    if base is None:
        return None
    if device and device != base.device:
        base = replace(base, device=device, device_note=f"override: {device}")
    return profile, base.device, base


def list_profiles() -> int:
    print()
    print("Available local model choices")
    print()
    print(f"  {'command':<22} {'model':<36} {'device':<8}")
    print(f"  {'-' * 22} {'-' * 36} {'-' * 8}")
    for friendly in FRIENDLY:
        resolved = _resolve(friendly)
        if resolved is None:
            print(f"  {friendly:<22} {'(configuration unavailable)':<36} {'-':<8}")
            continue
        _profile, device, config = resolved
        print(f"  {friendly:<22} {config.model.split('/')[-1]:<36} {device:<8}")
    print()
    print("Start a chat with:")
    print("  .\\chat.ps1 qwen3-8b-npu")
    print()
    print("The three Qwen3-8B choices use the same model artifact and change only")
    print("the execution device. qwen3-coder-30b selects a different model profile.")
    print()
    return 0


def _header(name: str, profile: str, config: ModelConfig, reachable: bool) -> None:
    print()
    print(BANNER)
    print()
    print(f"  Selection   {name}")
    print(f"  Model       {config.model.split('/')[-1]}")
    print(f"  Device      {config.device}")
    print(f"  Backend     {RUNTIME_LABEL.get(config.runtime, config.runtime)}")
    print(f"  Endpoint    {config.base_url}")
    print(f"  Status      {'Ready' if reachable else 'Model server not running'}")
    print()
    if reachable:
        print("Type a message and press Enter. Use an empty line or Ctrl-C to exit.")
        print()


def _reachable(config: ModelConfig) -> bool:
    import urllib.error
    import urllib.request

    root = config.base_url.rstrip("/").rsplit("/", 1)[0]
    for url in (f"{config.base_url.rstrip('/')}/models", f"{root}/v1/models"):
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def converse(name: str, profile: str, config: ModelConfig) -> int:
    from local_agent.llm.client import OpenAICompatibleClient

    reachable = _reachable(config)
    _header(name, profile, config, reachable)
    if not reachable:
        print("The model configuration is known, but nothing is serving it yet.")
        print()
        print("Start the configured model server with:")
        print(f"  python measurement/serve.py start --profile {profile} --device {config.device}")
        print()
        print("Then run the same chat command again.")
        print()
        return 2

    client = OpenAICompatibleClient(config)
    history: list[dict[str, str]] = []
    while True:
        try:
            said = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not said:
            print()
            return 0

        history.append({"role": "user", "content": said})
        try:
            reply = client.chat(history)
        except Exception as exc:
            print(f"\nRequest failed: {type(exc).__name__}: {exc}\n")
            history.pop()
            continue

        text = (reply.content or "").strip()
        print()
        print(text if text else "(The model returned no visible answer.)")
        stats = reply.stats
        if stats.ttft_s is not None and stats.decode_tok_s is not None:
            print()
            print(f"  Device: {config.device}")
            print(f"  Time to first token: {stats.ttft_s:.2f} s")
            print(f"  Generation speed:    {stats.decode_tok_s:.1f} tokens/s")
        print()
        history.append({"role": "assistant", "content": text})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chat", description="Talk directly to a local model.")
    parser.add_argument("model", nargs="?", default="list",
                        help="model choice, or 'list' to show available choices")
    args = parser.parse_args(argv)

    if args.model == "list":
        return list_profiles()

    resolved = _resolve(args.model)
    if resolved is None:
        print(f"\nUnknown model choice: {args.model}\n", file=sys.stderr)
        list_profiles()
        return 2
    profile, _device, config = resolved
    return converse(args.model, profile, config)


if __name__ == "__main__":
    raise SystemExit(main())
