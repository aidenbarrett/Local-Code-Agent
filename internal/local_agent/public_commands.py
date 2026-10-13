"""Literal PowerShell commands for the supported checkout entrypoints."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

_PRODUCT_ROOT = Path(__file__).resolve().parents[2]


def powershell_literal(value: str | Path) -> str:
    """Quote data without PowerShell expansion, including apostrophes in paths."""
    return "'" + str(value).replace("'", "''") + "'"


def public_entrypoint(
    script: Literal["local-code-agent.ps1", "install.ps1"] = "local-code-agent.ps1",
) -> str:
    """Name the installed product script independently of the user's directory."""
    return "& " + powershell_literal(_PRODUCT_ROOT / script)
