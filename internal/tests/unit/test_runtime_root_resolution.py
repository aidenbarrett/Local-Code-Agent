"""Setup and the launcher agree on where runtime state lives.

Before this, install.ps1 took its own -RuntimeRoot while local-code-agent.ps1 always
overwrote LCA_RUNTIME_ROOT with LOCALAPPDATA, so a custom location installed cleanly
and then the launcher could not find OVMS, the weights or the stored model choice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from serving.model_choice import choice_path
from serving.model_store import default_runtime_root

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "internal" / "scripts" / "runtime-root.ps1"


def _powershell() -> str:
    found = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not found:
        assert os.name != "nt", "a Windows CI runner must provide PowerShell"
        pytest.skip("PowerShell is not installed on this non-Windows machine")
    return found


def _environment(**values: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for name, value in values.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


@pytest.mark.parametrize("case", ["explicit", "empty-explicit", "localappdata", "home"])
def test_the_powershell_rule_matches_the_python_owner(tmp_path, monkeypatch, case):
    values: dict[str, str | None] = {
        "explicit": {"LCA_RUNTIME_ROOT": str(tmp_path / "chosen"), "LOCALAPPDATA": str(tmp_path / "appdata")},
        "empty-explicit": {"LCA_RUNTIME_ROOT": "", "LOCALAPPDATA": str(tmp_path / "appdata")},
        "localappdata": {"LCA_RUNTIME_ROOT": None, "LOCALAPPDATA": str(tmp_path / "appdata")},
        "home": {"LCA_RUNTIME_ROOT": None, "LOCALAPPDATA": None},
    }[case]
    env = _environment(**values)
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f"Set-StrictMode -Version Latest; . '{HELPER}'; Resolve-LcaRuntimeRoot"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert Path(result.stdout.strip()) == default_runtime_root()


def test_no_entry_point_carries_its_own_runtime_location():
    """Every PowerShell script resolves through the one helper; none hard-codes the path."""
    offenders = []
    for script in REPO.rglob("*.ps1"):
        relative = script.relative_to(REPO)
        if relative.parts[0].startswith(".venv") or script == HELPER:
            continue
        text = re.sub(r"<#.*?#>", "", script.read_text(encoding="utf-8"), flags=re.S)
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#") and "Write-Host" not in line)
        if "LocalCodeAgent" in code.replace("Local Code Agent", "") and "Resolve-LcaRuntimeRoot" not in code:
            offenders.append(str(relative))
        if "LOCALAPPDATA" in code:
            offenders.append(f"{relative} (reads LOCALAPPDATA itself)")
    assert offenders == []


def test_setup_has_no_private_runtime_root_switch():
    install = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$RuntimeRoot" not in install
    assert "$RuntimeRoot = Resolve-LcaRuntimeRoot" in install
    assert "LCA_RUNTIME_ROOT" in install  # the help names the one supported setting


@pytest.mark.skipif(os.name != "nt", reason="the public launcher runs the Windows managed environment")
def test_the_launcher_honours_a_user_chosen_runtime_root(tmp_path):
    """`models use` through the public launcher stores the choice under LCA_RUNTIME_ROOT."""
    chosen = tmp_path / "chosen root"
    env = _environment(LCA_RUNTIME_ROOT=str(chosen), LOCALAPPDATA=str(tmp_path / "appdata"))
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(REPO / "local-code-agent.ps1"), "models", "use", "ptl-gpu-30b"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(choice_path(chosen).read_text(encoding="utf-8")) == {"preset": "ptl-gpu-30b"}
    assert not (tmp_path / "appdata").exists()
