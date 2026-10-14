"""Recovery commands work from another repository and preserve literal paths."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from local_agent import public_commands
from local_agent.repo_setup import init_command
from serving.serve import pull_command


def test_recovery_commands_target_the_product_not_the_open_repository(tmp_path, monkeypatch):
    product = tmp_path / "installed product"
    repository = tmp_path / "external repository"
    product.mkdir()
    repository.mkdir()
    monkeypatch.setattr(public_commands, "_PRODUCT_ROOT", product)
    monkeypatch.chdir(repository)
    assert init_command(repository) == (
        f"& '{product / 'local-code-agent.ps1'}' init --repo '{repository}'"
    )
    assert pull_command("ptl-gpu-30b") == (
        f"& '{product / 'local-code-agent.ps1'}' models pull ptl-gpu-30b"
    )
    assert public_commands.public_entrypoint("install.ps1") == f"& '{product / 'install.ps1'}'"


def test_generated_init_command_round_trips_literal_paths_through_powershell(tmp_path, monkeypatch):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    product = tmp_path / "product's $data` folder"
    repository = tmp_path / "repo's $data` folder"
    product.mkdir()
    repository.mkdir()
    (product / "local-code-agent.ps1").write_text(
        'param([string]$Command, [Parameter(ValueFromRemainingArguments=$true)][string[]]$Rest)\n'
        '@{command=$Command;arguments=$Rest} | ConvertTo-Json -Compress\n', encoding="utf-8",
    )
    monkeypatch.setattr(public_commands, "_PRODUCT_ROOT", product)
    command = init_command(repository)
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=repository, capture_output=True, text=True, check=True, timeout=20,
    )
    assert json.loads(result.stdout) == {"command": "init", "arguments": ["--repo", str(repository)]}
