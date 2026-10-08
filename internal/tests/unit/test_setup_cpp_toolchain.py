"""Setup installs and checks the C++ toolchain it promises.

install.ps1's plan named Kitware.CMake and the VS 2022 Build Tools, but the bootstrap
never checked or installed either. A clean machine passed every check and then died in
the fixture smoke test on "The term 'cmake' is not recognized" (first NUC install).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[3]
INSTALL = REPO / "install.ps1"
CORE = REPO / "internal" / "bootstrap-work-laptop-core.ps1"
MSVC = REPO / "internal" / "scripts" / "msvc.ps1"


def _planned_packages() -> list[str]:
    install = INSTALL.read_text(encoding="utf-8")
    plan = install[install.index("function Show-InstallPlan"):install.index("Package source:")]
    return re.findall(r"Write-Host '  ([A-Za-z0-9.]+)", plan)


def test_every_package_the_plan_names_is_installed_by_the_bootstrap():
    planned = _planned_packages()
    assert {"Git.Git", "Python.Python.3.12", "Kitware.CMake",
            "Microsoft.VisualStudio.2022.BuildTools"} <= set(planned)
    installed = set(re.findall(r'InstallWinget "([A-Za-z0-9.]+)"', CORE.read_text(encoding="utf-8")))
    assert set(planned) <= installed, sorted(set(planned) - installed)


def test_a_missing_toolchain_blocks_readiness_instead_of_failing_later():
    core = CORE.read_text(encoding="utf-8")
    assert 'Result "CMake" "FAIL" "not found"' in core
    assert 'Result "CMake" "FAIL" "installed but not on PATH"' in core
    assert 'Result "C++ compiler (MSVC)" "FAIL"' in core
    # Installs happen only when setup was approved, never in -CheckOnly.
    for package in ("Kitware.CMake", "Microsoft.VisualStudio.2022.BuildTools"):
        call = core.index(f'InstallWinget "{package}"')
        guard = core.rindex("!$CheckOnly -and $InstallMissing", 0, call)
        assert call - guard < 400, package
    # The one-shot's C++ smoke test runs only after the base stage passed.
    one_shot = (REPO / "internal" / "work-laptop-one-shot.ps1").read_text(encoding="utf-8")
    assert one_shot.index('Fail "base bootstrap returned') < one_shot.index("& cmake -S . -B build")


def test_setup_and_doctor_share_one_msvc_probe():
    """FindMsvc has one owner; setup and the doctor launcher both dot-source it."""
    core = CORE.read_text(encoding="utf-8")
    launcher = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "function FindMsvc" not in core and "function FindMsvc" not in launcher
    assert "$MsvcComponent = " not in core
    assert ". (Join-Path $PSScriptRoot 'scripts\\msvc.ps1')" in core
    assert "'scripts\\msvc.ps1'" in launcher
    assert "function FindMsvc" in MSVC.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="vswhere and MSVC exist only on Windows")
def test_the_msvc_probe_finds_the_runners_compiler():
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell, "a Windows CI runner must provide PowerShell"
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         f". '{MSVC}'; FindMsvc"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    found = result.stdout.strip()
    assert found and Path(found, "VC", "Tools", "MSVC").is_dir(), result.stdout + result.stderr
