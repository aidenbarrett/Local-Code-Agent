from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "runtime-preflight.py"


def _load_preflight():
    spec = spec_from_file_location("lca_runtime_preflight_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preflight_rejects_declared_dependency_missing_from_selected_interpreter(tmp_path):
    preflight = _load_preflight()
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        "name = 'preflight-fixture'\n"
        "version = '0.0.0'\n"
        "dependencies = ['definitely-not-an-installed-lca-package>=1']\n",
        encoding="utf-8",
    )

    errors = preflight.runtime_errors(pyproject, critical_imports=())

    assert errors == [
        "declared runtime dependency is not installed: definitely-not-an-installed-lca-package"
    ]


def test_preflight_accepts_exact_ci_interpreter_after_project_install():
    preflight = _load_preflight()

    assert preflight.runtime_errors(REPO / "pyproject.toml") == []


def test_public_launcher_runs_runtime_preflight_before_dispatch():
    launcher = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")

    preflight_call = launcher.index("& $python $preflight --pyproject $pyproject")
    dispatch = launcher.index("switch ($Command.ToLowerInvariant())")
    run_task = launcher.index("run-task-ui.py")

    assert preflight_call < dispatch < run_task
    assert "pip install" not in launcher


def test_public_install_check_uses_managed_interpreter_for_same_preflight():
    installer = (REPO / "install.ps1").read_text(encoding="utf-8")

    assert ".venv-workstation\\Scripts\\python.exe" in installer
    assert "& $managedPython $runtimePreflight --pyproject $pyproject" in installer
