"""Shared human-facing terminal presentation for Local Code Agent.

This module is deliberately outside ``internal/local_agent`` so changing product
presentation does not move the measured agent source identity. It contains no
policy, routing, model, tool, verification or experiment behaviour.

The visual direction is intentionally terminal-native: a restrained neon palette,
strong section rails, and one static LCA mark that renders consistently in modern
monospace terminals. Colour is additive only; redirected output and NO_COLOR stay
plain and readable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

WIDTH = 72

# Keep every row exactly the same display width. The regression test treats this
# as a product contract because a crooked logo is immediately visible in a demo.
LCA_LOGO = (
    "██╗      ██████╗    █████╗",
    "██║     ██╔════╝  ██╔══██╗",
    "██║     ██║       ███████║",
    "██║     ██║       ██╔══██║",
    "███████╗╚██████╗  ██║  ██║",
    "╚══════╝ ╚═════╝  ╚═╝  ╚═╝",
)

DEVICE_LABELS = {
    "NPU": "NPU · laptop AI accelerator",
    "GPU": "GPU · graphics processor",
    "CPU": "CPU · general-purpose processor",
}

_RGB = {
    "cyan": (0, 255, 240),
    "magenta": (255, 54, 226),
    "amber": (255, 184, 64),
    "green": (80, 255, 150),
    "red": (255, 82, 110),
    "dim": (135, 145, 165),
    "white": (235, 240, 246),
}


def _colour_enabled(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def device_label(device: str) -> str:
    """Human-facing hardware name without hiding the canonical device token."""
    return DEVICE_LABELS.get(device.upper(), device.upper())


@dataclass
class TerminalUI:
    stream: TextIO = sys.stdout
    colour: bool | None = None
    width: int = WIDTH

    def __post_init__(self) -> None:
        if self.colour is None:
            self.colour = _colour_enabled(self.stream)
        if self.width < 48:
            raise ValueError("terminal UI width must be at least 48 columns")

    def paint(self, text: str, role: str, *, bold: bool = False) -> str:
        if not self.colour:
            return text
        r, g, b = _RGB[role]
        weight = "1;" if bold else ""
        return f"\x1b[{weight}38;2;{r};{g};{b}m{text}\x1b[0m"

    def line(self, text: str = "") -> None:
        print(text, file=self.stream)

    def rule(self, char: str = "═", *, role: str = "cyan") -> None:
        self.line(self.paint(char * self.width, role))

    def banner(self, title: str, subtitle: str | None = None) -> None:
        self.line()
        for row in LCA_LOGO:
            self.line("  " + self.paint(row, "cyan", bold=True))
        self.line()
        self.line("  " + self.paint("LOCAL CODE AGENT", "magenta", bold=True))
        self.line()
        self.rule("═", role="cyan")
        self.line("  " + self.paint(title.upper(), "amber", bold=True))
        if subtitle:
            self.line("  " + subtitle)
        self.rule("═", role="cyan")
        self.line()

    def section(self, title: str) -> None:
        label = f"[ {title.upper()} ]"
        fill = max(0, self.width - len(label) - 3)
        rail = "╠═" + label + "═" * fill
        self.line(self.paint(rail, "cyan", bold=True))
        self.line()

    def field(self, label: str, value: str, *, role: str | None = None) -> None:
        rendered = self.paint(value, role, bold=role in {"green", "red"}) if role else value
        self.line(f"  {label:<18} {rendered}")

    def status(self, state: str, text: str) -> None:
        state = state.lower()
        marks = {
            "ok": ("✓", "green"),
            "warn": ("!", "amber"),
            "fail": ("×", "red"),
            "active": ("◆", "cyan"),
            "info": ("·", "dim"),
        }
        mark, role = marks.get(state, marks["info"])
        self.line(f"  {self.paint(mark, role, bold=True)} {text}")

    def request_header(self, number: int, state: str) -> None:
        role = "amber" if state == "COLD START" else "cyan"
        text = f"REQUEST {number:02d}  //  {state}"
        self.line("  " + self.paint(text, role, bold=True))

    def footer_note(self, text: str) -> None:
        self.line()
        self.line("  " + self.paint(text, "dim"))


def ui(*, stream: TextIO | None = None, colour: bool | None = None,
       width: int = WIDTH) -> TerminalUI:
    target = sys.stdout if stream is None else stream
    return TerminalUI(stream=target, colour=colour, width=width)
