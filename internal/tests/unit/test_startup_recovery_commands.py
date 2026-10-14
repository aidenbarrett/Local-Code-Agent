"""Broken startup still gives a usable installer command from another repository."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
PREFLIGHT = REPO / "internal/scripts/runtime-preflight.py"


def test_preflight_recovery_uses_its_checkout_without_product_imports(tmp_path, monkeypatch, capsys):
    spec = spec_from_file_location("startup_recovery_preflight", PREFLIGHT)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    product = tmp_path / "product's $folder"
    product.mkdir()
    monkeypatch.setattr(module, "runtime_errors", lambda _: ["broken runtime fixture"])
    assert module.main(["--pyproject", str(product / "pyproject.toml")]) == 2
    error = capsys.readouterr().err
    installer = str(product / "install.ps1").replace("'", "''")
    assert f"Run & '{installer}' to update the managed checkout environment." in error
    assert "Run .\\install.ps1" not in error
    assert not (product / "install.ps1").exists(), "the diagnostic tried to repair startup"


@pytest.mark.skipif(os.name != "nt", reason="supported Windows public launcher")
def test_public_failed_startup_names_the_installer_from_an_external_directory(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "Windows CI requires PowerShell"
    product = tmp_path / "product's $folder"
    scripts = product / "internal/scripts"
    scripts.mkdir(parents=True)
    external = tmp_path / "external repository"
    external.mkdir()
    shutil.copy2(REPO / "local-code-agent.ps1", product / "local-code-agent.ps1")
    shutil.copy2(PREFLIGHT, scripts / "runtime-preflight.py")
    (product / "pyproject.toml").write_text(
        '[project]\nname="startup-recovery"\n'
        'dependencies=["definitely-not-an-installed-lca-package>=1"]\n', encoding="utf-8",
    )
    marker = product / "unexpected-install.txt"
    (product / "install.ps1").write_text(
        "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'unexpected-install.txt') -Value ran\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(product / "local-code-agent.ps1"), "help"],
        cwd=external, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    installer = str(product / "install.ps1").replace("'", "''")
    assert f"Run & '{installer}' to update the checkout environment, then retry." in result.stdout
    assert f"Run & '{installer}' to update the managed checkout environment." in result.stderr
    assert "Run .\\install.ps1" not in result.stdout + result.stderr
    assert not marker.exists()
