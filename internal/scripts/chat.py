#!/usr/bin/env python3
r"""Talk directly to a local model. No agent, tools, or repository access.

Use the root PowerShell entrypoint on Windows:

    .\chat.ps1
    .\chat.ps1 qwen3-8b-npu

This is the simplest user-facing path through the project: choose a configured
model/device profile and have a normal terminal conversation. Local Code Agent
is separate; it adds controlled repository access, restricted tools, skills and
independent verification.

The chat path uses the same client, model profiles and serving controller as the
rest of the project. There is no demo-only model path.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import os
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from local_agent.config import MODEL_PRESETS, ModelConfig  # noqa: E402
from measurement import serve  # noqa: E402

FRIENDLY: dict[str, tuple[str, str]] = {
    "qwen3-8b-npu": ("ptl-npu-8b", "NPU"),
    "qwen3-8b-gpu": ("ptl-npu-8b", "GPU"),
    "qwen3-8b-cpu": ("ptl-npu-8b", "CPU"),
    "qwen3-coder-30b": ("ptl-gpu-30b", "GPU"),
}

BANNER = "Local Model Chat"
RUNTIME_LABEL = {
    "ovms": "OpenVINO Model Server",
    "llamacpp": "llama.cpp",
    "cloud": "Cloud",
}


def _resolve(name: str) -> tuple[str, str, ModelConfig] | None:
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


def _model_label(config: ModelConfig) -> str:
    artifact = config.model.split("/")[-1]
    if artifact == "Qwen3-8B-int4-cw-ov":
        return "Qwen3-8B (INT4)"
    return artifact


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
        print(f"  {friendly:<22} {_model_label(config):<36} {device:<8}")
    print()
    print("Start a chat with:")
    print("  .\\chat.ps1 qwen3-8b-npu")
    print()
    print("The three Qwen3-8B choices use the same model artifact and change only")
    print("the execution device. qwen3-coder-30b selects a different model profile.")
    print()
    return 0


def _header(name: str, config: ModelConfig) -> None:
    print()
    print(BANNER)
    print()
    print(f"  Selection   {name}")
    print(f"  Model       {_model_label(config)}")
    print(f"  Device      {config.device}")
    print(f"  Backend     {RUNTIME_LABEL.get(config.runtime, config.runtime)}")
    print(f"  Status      Ready")
    print()
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


def _runtime_root() -> Path:
    explicit = os.environ.get("LCA_RUNTIME_ROOT")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalCodeAgent"


def _ensure_server(profile: str, config: ModelConfig) -> bool:
    """Reuse a compatible owned server or start one through the serving controller."""
    if _reachable(config):
        return True

    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    plan = serve.make_plan(
        profile,
        config,
        _runtime_root(),
        executable=executable,
    )

    record = serve.read_record(plan)
    if record:
        state = serve.status(plan)
        record_device = (
            ((record.get("plan") or {}).get("model_configuration") or {}).get("device")
        )
        if state.get("healthy") and record_device == config.device:
            return True
        if state.get("process_alive"):
            print(f"Stopping the previous {record_device or 'local'} model server...")
            serve.stop(plan)

    print(f"Starting {_model_label(config)} on {config.device}...")
    try:
        state = serve.start(plan, config, wait_seconds=900)
    except (serve.Refusal, OSError) as exc:
        print()
        print("Model server could not be started.", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print("Run the root setup command and try again:", file=sys.stderr)
        print("  .\\install.ps1", file=sys.stderr)
        print(file=sys.stderr)
        return False

    if not state.get("healthy"):
        print("Model server did not become ready.", file=sys.stderr)
        return False
    print("Model server ready.")
    return True


def _system_message(config: ModelConfig) -> dict[str, str]:
    backend = RUNTIME_LABEL.get(config.runtime, config.runtime)
    return {
        "role": "system",
        "content": (
            "You are a language model running entirely on this machine, with no network "
            f"access. You are served by {backend} on the {config.device} of the user's "
            f"computer. The model is {_model_label(config)}.\n\n"
            "You are running inside a plain terminal chat program. There is no window, "
            "no button and no menu. The person types a line and presses Enter. To leave, "
            "they press Enter on an empty line, or Ctrl-C.\n\n"
            "You have no tools, no access to the filesystem, and no ability to run "
            "commands. This program is separate from Local Code Agent, which is the part "
            "of this project that gives a model controlled repository access and verifies "
            "its work independently. You are not that, and you cannot speak for it.\n\n"
            "If you are asked something about this program, this project or this machine "
            "that you have not been told here, say you do not know. Do not guess at "
            "feature names, buttons or commands."
        ),
    }


def converse(name: str, profile: str, config: ModelConfig) -> int:
    from local_agent.llm.client import OpenAICompatibleClient

    if not _ensure_server(profile, config):
        return 2
    _header(name, config)

    client = OpenAICompatibleClient(config)
    history: list[dict[str, str]] = [_system_message(config)]
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
    parser.add_argument("model", nargs="?", default="list", help="model choice, or 'list' to show available choices")
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
