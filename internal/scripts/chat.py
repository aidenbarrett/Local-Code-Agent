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
        _profile, _device, config = resolved
        term.field(friendly, f"{device_label(config.device)} · {_model_label(config)}")
    term.line()
    return 0


def _session_header(name: str, config: ModelConfig, term, persona: Persona | None = None) -> None:
    term.section("CHAT SESSION")
    term.field("Model", _model_label(config))
    term.field("Device", device_label(config.device))
    term.field("Runtime", RUNTIME_LABEL.get(config.runtime, config.runtime))
    term.field("Choice", name)
    term.field("Persona", f"{persona.name} v{persona.version}" if persona else "off")
    term.line()


def _ensure_server(profile: str, config: ModelConfig, *, term) -> bool:
    if config.runtime == "cloud":
        return True
    term.status("info", "Checking model server")
    try:
        ready = serve.ensure_server(profile, config)
    except Exception as exc:
        term.status("fail", f"Model server failed: {type(exc).__name__}: {exc}")
        return False
    if not ready:
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


def _messages_for_turn(
    history: list[dict[str, str]],
    said: str,
    persona: Persona | None,
    config: ModelConfig,
) -> list[dict[str, str]]:
    """Return the exact message list handed to the client for this turn.

    ``history`` contains conversation turns only. Derived controller contract and
    optional persona messages are composed afresh for every request, so future
    persistence cannot accidentally freeze either into the session record.
    """
    contract = [_system_message(config)]
    tone = [persona_message(persona)] if persona is not None else []
    current_turn = [{"role": "user", "content": said}]
    return [*contract, *tone, *history, *current_turn]


def _load_requested_persona(value: str | None, term) -> Persona | None:
    if not value or value.lower() == "off":
        return None
    if value.lower() == "neutral":
        path = SOURCE_ROOT / "personas" / "neutral.toml"
    else:
        path = Path(value).expanduser()
    try:
        return load_persona(path)
    except PersonaError as exc:
        term.status("warn", f"Persona disabled: {exc}")
        return None


def converse(
    name: str,
    profile: str,
    config: ModelConfig,
    persona: Persona | None = None,
) -> int:
    from local_agent.llm.client import OpenAICompatibleClient

    term = ui()
    term.banner(
        "LOCAL MODEL CHAT",
        "Runs locally on this computer. Repository access is not enabled in chat.",
    )
    term.section("MODEL STARTUP")
    if not _ensure_server(profile, config, term=term):
        return 2
    _session_header(name, config, term, persona)

    client = OpenAICompatibleClient(config)
    history: list[dict[str, str]] = []
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

        messages = _messages_for_turn(history, said, persona, config)
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
            term.line(
                "  "
                + term.paint(config.device, "cyan", bold=True)
                + f" · first token {stats.ttft_s:.2f} s"
                + f" · {stats.decode_tok_s:.1f} tokens/s"
            )
        term.line()
        history.append({"role": "user", "content": said})
        history.append({"role": "assistant", "content": text})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chat", description="Talk directly to a local model.")
    parser.add_argument("model", nargs="?", default="list", help="model choice, or 'list' to show available choices")
    parser.add_argument(
        "--persona",
        default="off",
        metavar="NAME_OR_PATH",
        help="optional chat-only tone profile: 'neutral', 'off', or a persona TOML path",
    )
    args = parser.parse_args(argv)
    if args.model == "list":
        return list_profiles()
    resolved = _resolve(args.model)
    if resolved is None:
        print(f"\nUnknown model choice: {args.model}\n", file=sys.stderr)
        list_profiles()
        return 2
    profile, _device, config = resolved
    term = ui()
    persona = _load_requested_persona(args.persona, term)
    return converse(args.model, profile, config, persona)


if __name__ == "__main__":
    raise SystemExit(main())
