#!/usr/bin/env python3
"""Human-facing Local Code Agent command overview."""
from __future__ import annotations

import sys
from pathlib import Path

INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from terminal_ui import ui  # noqa: E402


def main() -> int:
    term = ui()
    term.banner(
        "CONTROLLED CODING AGENT",
        "Local Code Agent gives a model controlled repository access and independent verification.",
    )

    term.section("DIRECT MODEL CHAT")
    term.line("  Chat gives direct access to the local model without repository tools.")
    term.line(r"    .\chat.ps1 qwen3-8b-npu")

    term.line()
    term.section("COMMANDS")
    term.field("session", "Continuous conversation and controlled tasks (prototype; server must be running)")
    term.field("capabilities", "Show what the agent can and cannot do")
    term.field("run-task", "Run a controlled engineering task against this repository")
    term.field("verification-demo", "Show stale passing tests being rejected as invalid evidence")
    term.field("advanced", "Pass arguments directly to the underlying developer CLI")

    term.line()
    term.section("EXAMPLES")
    term.line(r"  .\local-code-agent.ps1 capabilities")
    term.line(r'  .\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation')
    term.line(r"  .\local-code-agent.ps1 verification-demo")
    term.line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
