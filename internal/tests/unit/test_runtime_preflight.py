from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "runtime-preflight.py"


def _load_preflight():
    spec = spec_from_file_location("lca_runtime_preflight_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _kill_process_tree(pid: int) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Freeze, inventory and stop the timed-out launcher process tree."""
    try:
        parent = psutil.Process(pid)
        parent.suspend()
        descendants = parent.children(recursive=True)
    except psutil.Error:
        return (), ()
    observed: list[str] = []
    for child in descendants:
        try:
            observed.append(f"pid={child.pid} name={child.name()} status={child.status()}")
            child.kill()
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass
    _, alive = psutil.wait_procs([parent, *descendants], timeout=5)
    return tuple(observed), tuple(child.pid for child in alive if child.is_running())


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

    assert launcher.index("'interpreter-selected'") < launcher.index("'preflight-start'")
    assert launcher.index("'preflight-start'") < launcher.index("'preflight-finished'")
    assert launcher.index("'help-start'") < launcher.index("'help-finished'")


def test_preflight_trace_names_the_import_boundary(monkeypatch, capsys):
    monkeypatch.setenv("LCA_STARTUP_TRACE", "1")
    preflight = _load_preflight()

    assert preflight.runtime_errors(REPO / "pyproject.toml", critical_imports=("json",)) == []

    trace = capsys.readouterr().err
    assert "stage=runtime-contract-start" in trace
    assert "stage=import-start detail=json" in trace
    assert "stage=import-finished detail=json" in trace
    assert "stage=runtime-contract-finished detail=errors=0" in trace


def test_public_install_check_uses_managed_interpreter_for_same_preflight():
    installer = (REPO / "install.ps1").read_text(encoding="utf-8")

    assert ".venv-workstation\\Scripts\\python.exe" in installer
    assert "& $managedPython $runtimePreflight --pyproject $pyproject" in installer


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows PowerShell acceptance")
def test_windows_public_launcher_preflights_real_product_help():
    env = dict(os.environ)
    env["LCA_STARTUP_TRACE"] = "1"
    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO / "local-code-agent.ps1"),
            "help",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired as exc:
        observed, survivors = _kill_process_tree(process.pid)
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            "public launcher exceeded 30s; owned descendants before cleanup: "
            f"{observed or ('none observed',)}\n"
            f"descendants surviving cleanup: {survivors or ('none',)}\n"
            f"stdout before timeout:\n{exc.stdout or stdout}\n"
            f"stderr/startup trace before timeout:\n{exc.stderr or stderr}"
        )

    assert process.returncode == 0, stdout + stderr
    assert "Local Code Agent runtime preflight failed" not in stdout + stderr
    assert "stage=interpreter-selected" in stderr
    assert "stage=preflight-finished detail=exit=0" in stderr
    assert "stage=help-finished detail=exit=0" in stderr
