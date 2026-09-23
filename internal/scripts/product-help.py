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
        "LOCAL CODE AGENT",
        "One local product surface for conversation, controlled repository work and verification.",
    )

    term.section("START")
    term.field(r".\local-code-agent.ps1", "Open the Session Hub (default human interface)")
    term.line()

    term.section("COMMANDS")
    term.field("chat", "Raw local-model chat only; no repository tools or verification")
    term.field("capabilities", "Show what controlled repository work is available")
    term.field("run-task", "Run one controlled engineering task headlessly")
    term.field("verification-demo", "Show stale passing tests being rejected as invalid evidence")
    term.field("advanced", "Pass arguments directly to the underlying developer CLI")

    term.line()
    term.section("EXAMPLES")
    term.line(r"  .\local-code-agent.ps1")
    term.line(r"  .\local-code-agent.ps1 chat qwen3-8b-npu")
    term.line(r"  .\local-code-agent.ps1 capabilities")
    term.line(r'  .\local-code-agent.ps1 run-task "Inspect this repository and summarize how it builds" --skill repo-navigation')
    term.line()
    term.footer_note("The Session Hub is the product surface. Lower-level commands remain available for automation and diagnostics.")
    term.line()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
