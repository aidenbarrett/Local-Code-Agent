#!/usr/bin/env python3
r"""Talk directly to a local model. No agent, tools, or repository access.

Use the root PowerShell entrypoint on Windows:

    .\chat.ps1
    .\chat.ps1 qwen3-8b-npu
    .\chat.ps1 qwen3-8b-npu --persona neutral

This is the simplest user-facing path through the project: choose a configured
model/device profile and have a normal terminal conversation. Local Code Agent
is separate; it adds controlled repository access, restricted tools, skills and
independent verification.

The chat path uses the same client, model profiles and serving controller as the
rest of the project. There is no demo-only model path. Optional persona support
is deliberately chat-only and does not enter agent or evaluation execution.
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
from scripts.chat_persona import Persona, PersonaError, load_persona, persona_message  # noqa: E402
from terminal_ui import device_label, ui  # noqa: E402

FRIENDLY: dict[str, tuple[str, str]] = {
    "qwen3-8b-npu": ("ptl-npu-8b", "NPU"),
    "qwen3-8b-gpu": ("ptl-npu-8b", "GPU"),
    "qwen3-8b-cpu": ("ptl-npu-8b", "CPU"),
}

CHAT_DEFAULT_TEMPERATURE = 0.7

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
    term = ui()
    term.banner(
        "LOCAL MODEL CHAT",
        "Available local model choices use the same Qwen3-8B on different hardware.",
    )
    term.section("AVAILABLE LOCAL MODEL CHOICES")
    for friendly in FRIENDLY:
        resolved = _resolve(friendly)
        if resolved is None:
            term.field(friendly, "configuration unavailable", role="red")
            continue
        _profile, device, config = resolved
        term.field(friendly, f"{_model_label(config)} · {device_label(device)}")
    term.line()
    term.section("START CHAT")
    term.line(r"  .\chat.ps1 qwen3-8b-npu")
    term.line(r"  .\chat.ps1 qwen3-8b-npu --persona neutral")
    term.footer_note("Chat is direct model conversation. Repository access is not enabled here.")
    term.line()
    return 0


def _session_header(name: str, config: ModelConfig, term, persona: Persona | None = None) -> None:
    term.line()
    term.section("CHAT SESSION")
    term.field("Selection", name)
    term.field("Model", _model_label(config))
    term.field("Running on", device_label(config.device), role="cyan")
    term.field("Backend", RUNTIME_LABEL.get(config.runtime, config.runtime))
    term.field("Persona", persona.label if persona else "off")
    term.field("Temperature", f"{config.temperature:g} · chat-only")
    term.field("Status", "Ready", role="green")
    term.line()
    term.status("info", "Direct chat only · no repository access, tools or verification")
    term.status("info", "Empty line or Ctrl-C at the prompt exits")
    term.status("info", "Ctrl-C while generating stops that reply and returns to the prompt")
    term.line()


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


def _ensure_server(profile: str, config: ModelConfig, term=None) -> bool:
    """Reuse only an owned compatible server; otherwise start through the controller."""
    term = term or ui()
    executable = os.environ.get("LCA_OVMS_EXECUTABLE") if config.runtime == "ovms" else None
    plan = serve.make_plan(profile, config, _runtime_root(), executable=executable)
    record = serve.read_record(plan)
    if record:
        state = serve.status(plan)
        recorded = (record.get("plan") or {}).get("model_configuration") or {}
        record_device = recorded.get("device")
        record_model = recorded.get("model")
        if state.get("healthy") and record_device == config.device and record_model == config.model:
            term.status("ok", f"Local model server already ready on {config.device}")
            return True
        if state.get("process_alive"):
            term.status("info", f"Stopping previous {record_device or 'local'} model server")
            serve.stop(plan)
    if _reachable(config):
        term.line()
        term.status("warn", "The configured local endpoint is already in use")
        term.line("  That server is not owned by Local Code Agent, so it will not be adopted or stopped.")
        term.line("  Stop that server, then run this chat command again.")
        term.line()
        return False
    term.status("active", f"Starting {_model_label(config)} on {device_label(config.device)}")
    try:
        state = serve.start(plan, config, wait_seconds=900)
    except (serve.Refusal, OSError) as exc:
        term.line()
        term.status("fail", "Model server could not be started")
        term.line(f"  {exc}")
        term.line()
        term.line("  Run the root setup command and try again:")
        term.line(r"    .\install.ps1")
        term.line()
        return False
    if not state.get("healthy"):
        term.status("fail", "Model server did not become ready")
        return False
    term.status("ok", "Model server ready")
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
            "they press Enter on an empty line, or Ctrl-C while at the prompt.\n\n"
            "You have no tools, no access to the filesystem, and no ability to run "
            "commands. This program is separate from Local Code Agent, which is the part "
            "of this project that gives a model controlled repository access and verifies "
            "its work independently. You are not that, and you cannot speak for it.\n\n"
            "If you are asked something about this program, this project or this machine "
            "that you have not been told here, say you do not know. Do not guess at "
            "feature names, buttons or commands."
        ),
    }


def _messages_for_turn(history: list[dict[str, str]], said: str, persona: Persona | None) -> list[dict[str, str]]:
    messages = list(history)
    if persona is not None:
        messages.insert(1, persona_message(persona))
    messages.append({"role": "user", "content": said})
    return messages


def _load_requested_persona(value: str | None, term) -> Persona | None:
    if not value or value.lower() == "off":
        return None
    named = {
        "neutral": SOURCE_ROOT / "personas" / "neutral.toml",
        "aiden": SOURCE_ROOT / "personas" / "aiden.toml",
    }
    if value.lower() in named:
        path = named[value.lower()]
    else:
        path = Path(value).expanduser()
    try:
        return load_persona(path)
    except PersonaError as exc:
        term.status("warn", f"Persona disabled: {exc}")
        return None


def converse(name: str, profile: str, config: ModelConfig, persona: Persona | None = None) -> int:
    from local_agent.llm.client import OpenAICompatibleClient
    term = ui()
    term.banner("LOCAL MODEL CHAT", "Runs locally on this computer. Repository access is not enabled in chat.")
    term.section("MODEL STARTUP")
    if not _ensure_server(profile, config, term=term):
        return 2
    _session_header(name, config, term, persona)
    client = OpenAICompatibleClient(config)
    history: list[dict[str, str]] = [_system_message(config)]
    while True:
        try:
            prompt = term.paint("YOU  › ", "magenta", bold=True)
            said = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not said:
            print()
            return 0
        messages = _messages_for_turn(history, said, persona)
        try:
            reply = client.chat(messages)
        except KeyboardInterrupt:
            term.line()
            term.status("warn", "Generation stopped. Back at the prompt.")
            term.line()
            continue
        except Exception as exc:
            term.line()
            term.status("fail", f"Request failed: {type(exc).__name__}: {exc}")
            term.line()
            continue
        text = (reply.content or "").strip()
        term.line()
        term.line(term.paint("MODEL", "cyan", bold=True))
        term.line()
        term.line(text if text else "(The model returned no visible answer.)")
        stats = reply.stats
        if stats.ttft_s is not None and stats.decode_tok_s is not None:
            term.line()
            term.line("  " + term.paint(config.device, "cyan", bold=True) + f" · first token {stats.ttft_s:.2f} s" + f" · {stats.decode_tok_s:.1f} tokens/s")
        term.line()
        history.append({"role": "user", "content": said})
        history.append({"role": "assistant", "content": text})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chat", description="Talk directly to a local model.")
    parser.add_argument("model", nargs="?", default="list", help="model choice, or 'list' to show available choices")
    parser.add_argument("--persona", default="off", metavar="NAME_OR_PATH", help="optional chat-only tone profile: 'aiden', 'neutral', 'off', or a persona TOML path")
    parser.add_argument("--temperature", type=float, default=CHAT_DEFAULT_TEMPERATURE, help=f"chat-only sampling temperature (default: {CHAT_DEFAULT_TEMPERATURE:g}); agent/evaluation profiles are unchanged")
    parser.add_argument("--ensure-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.model == "list":
        return list_profiles()
    resolved = _resolve(args.model)
    if resolved is None:
        print(f"\nUnknown model choice: {args.model}\n", file=sys.stderr)
        list_profiles()
        return 2
    profile, _device, config = resolved
    if not 0.0 <= args.temperature <= 2.0:
        parser.error("--temperature must be between 0.0 and 2.0")
    config = replace(config, temperature=args.temperature)
    if args.ensure_only:
        return 0 if _ensure_server(profile, config) else 2
    term = ui()
    persona = _load_requested_persona(args.persona, term)
    return converse(args.model, profile, config, persona)


if __name__ == "__main__":
    raise SystemExit(main())
