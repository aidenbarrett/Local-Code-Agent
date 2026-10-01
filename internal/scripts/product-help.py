#!/usr/bin/env python3
"""Human-facing Local Code Agent command overview."""
from __future__ import annotations

import sys
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent.config import DEFAULT_MODEL_PRESET, MODEL_PRESETS  # noqa: E402
from terminal_ui import ui  # noqa: E402


def main() -> int:
    term = ui()
    term.banner(
        "LOCAL CODE AGENT",
        "One local product surface for conversation, controlled repository work and verification.",
    )

    term.section("START")
    term.field(r".\local-code-agent.ps1", "Open the Session Hub (default human interface)")
    default = MODEL_PRESETS[DEFAULT_MODEL_PRESET]
    term.field("Default worker", f"{DEFAULT_MODEL_PRESET} · {default.model} on {default.device}")
    term.line()

    term.section("COMMANDS")
    term.field("chat", "Raw local-model chat only; no repository tools or verification")
    term.field("capabilities", "Show what controlled repository work is available")
    term.field("run-task", "Run one controlled engineering task headlessly")
    term.field("verification-demo", "Show stale passing tests being rejected as invalid evidence")
    term.field("acceptance", "Run the acceptance journeys against this machine's model and keep every log")
    term.field("models", "List models; 'models use <profile>' picks yours, 'models pull <profile>' downloads one")
    term.field("advanced", "Pass arguments directly to the underlying developer CLI")

    term.line()
    term.section("EXAMPLES")
    term.line(r"  .\local-code-agent.ps1")
    term.line(r"  .\local-code-agent.ps1 chat qwen3-8b-npu")
    term.line(r"  .\local-code-agent.ps1 capabilities")
    term.line(r"  .\local-code-agent.ps1 acceptance --output C:\lca-acc --allow-model")
    term.line(r"  .\local-code-agent.ps1 models")
    term.line(r"  .\local-code-agent.ps1 models pull ptl-gpu-30b")
    term.line(r"  .\local-code-agent.ps1 models use ptl-gpu-30b")
    term.line(r"  .\local-code-agent.ps1 session --profile ptl-npu-8b")
    term.line(r'  .\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation')
    term.line()
    term.footer_note("The Session Hub is the product surface. Lower-level commands remain available for automation and diagnostics.")
    term.line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
