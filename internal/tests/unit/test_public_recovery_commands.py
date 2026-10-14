"""Recovery commands work from another repository and preserve literal paths."""
from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
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


_PRODUCT_SOURCES = ("internal/scripts", "internal/local_agent", "internal/serving")
_CWD_RELATIVE = (".\\local-code-agent.ps1", ".\\install.ps1")


def _docstring_ids(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, owners) and node.body
        and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }


def test_no_product_message_names_a_command_relative_to_the_users_directory():
    """A printed next step must run from wherever the user is, not only from the checkout."""
    root = Path(__file__).resolve().parents[3]
    offenders = []
    for base in _PRODUCT_SOURCES:
        for path in sorted((root / base).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = _docstring_ids(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings
                        and any(text in node.value for text in _CWD_RELATIVE)):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], "use public_commands.public_entrypoint(): " + ", ".join(offenders)


def _load_script(name: str):
    script = Path(__file__).resolve().parents[2] / "scripts" / f"{name}.py"
    spec = spec_from_file_location(f"recovery_{name.replace('-', '_')}", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_an_invalid_declaration_names_the_installed_doctor(tmp_path, monkeypatch, capsys):
    product = tmp_path / "installed product"
    repository = tmp_path / "external repository"
    product.mkdir()
    (repository / ".git").mkdir(parents=True)
    (repository / ".local-agent.toml").write_text("[profiles\n", encoding="utf-8")
    hub = _load_script("session-hub")
    monkeypatch.setattr(public_commands, "_PRODUCT_ROOT", product)
    monkeypatch.setattr(hub, "_LCA", public_commands.public_entrypoint())
    monkeypatch.chdir(repository)

    assert hub.main(["--repo", str(repository), "--profile", "ptl-npu-8b"]) == 2

    error = capsys.readouterr().err
    assert f"then check it with & '{product / 'local-code-agent.ps1'}' doctor" in error
    assert ".\\local-code-agent.ps1" not in error


def test_a_models_refusal_names_the_installed_command(tmp_path, monkeypatch, capsys):
    product = tmp_path / "installed product"
    product.mkdir()
    weights = _load_script("model_weights")
    monkeypatch.setattr(public_commands, "_PRODUCT_ROOT", product)
    monkeypatch.setattr(weights, "_LCA", public_commands.public_entrypoint())
    monkeypatch.chdir(tmp_path)

    assert weights.main(["pull", "no-such-profile", "--runtime-root", str(tmp_path / "rt")]) == 2

    error = capsys.readouterr().err
    assert f"run & '{product / 'local-code-agent.ps1'}' models" in error
