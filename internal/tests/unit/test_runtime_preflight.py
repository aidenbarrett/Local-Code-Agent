from __future__ import annotations

import contextlib
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import cast
from unittest.mock import Mock

import psutil
import pytest

from local_agent.tools import windows_job


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "internal" / "scripts" / "runtime-preflight.py"


def _load_preflight():
    spec = spec_from_file_location("lca_runtime_preflight_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _LauncherTimeout(RuntimeError):
    def __init__(
        self,
        *,
        observed: tuple[str, ...],
        survivors: tuple[str, ...],
        stdout: str,
        stderr: str,
        cleanup_confirmed: bool,
    ) -> None:
        super().__init__("owned launcher exceeded its time bound")
        self.observed = observed
        self.survivors = survivors
        self.stdout = stdout
        self.stderr = stderr
        self.cleanup_confirmed = cleanup_confirmed


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _merge_capture(original: str, later: str | bytes | None) -> str:
    latest = _text(later)
    if not latest:
        return original
    if original and original not in latest:
        return original + latest
    return latest


def _stop_owned_launcher(
    job: windows_job.ProcessTreeJob,
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    try:
        active = job.active_processes()
    except windows_job.JobContainmentError:
        active = -1
    observed = (f"job active_processes={active}",)
    confirmed = job.terminate_and_confirm(timeout_s=5)
    if confirmed:
        survivors = ()
    else:
        try:
            survivors = (f"job active_processes={job.active_processes()}",)
        except windows_job.JobContainmentError:
            survivors = ("job active_processes=unknown",)
    return observed, survivors, confirmed


def _process_identity_exists(pid: int, create_time: float) -> bool:
    """Return True only while the process originally observed at *pid* still exists."""
    try:
        return psutil.Process(pid).create_time() == create_time
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        # An observation failure cannot prove that the original process exited.
        return True


def _require_accounted_job_process(observed: tuple[str, ...]) -> int:
    """Require the known descendant to be inside the Job Object before cleanup."""
    assert observed, "Job Object process count was not observed before termination"
    active = int(observed[0].partition("=")[2])
    assert active >= 1, (
        "known child was not accounted to the Job Object before termination: "
        f"active_processes={active}"
    )
    return active


def _start_owned_launcher(
    command: list[str], *, cwd: Path, env: dict[str, str]
) -> tuple[subprocess.Popen[str], windows_job.ProcessTreeJob]:
    job = windows_job.ProcessTreeJob()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=windows_job.CREATE_SUSPENDED,
        )
        job.adopt_suspended(process.pid)
        return process, job
    except BaseException:
        try:
            if process is not None:
                with contextlib.suppress(Exception):
                    windows_job.terminate_unadopted(process, timeout_s=5)
                if process.stdout is not None:
                    with contextlib.suppress(Exception):
                        process.stdout.close()
                if process.stderr is not None:
                    with contextlib.suppress(Exception):
                        process.stderr.close()
        finally:
            job.close()
        raise


def _communicate_owned(
    process: subprocess.Popen[str],
    job: windows_job.ProcessTreeJob,
    *,
    timeout_s: float,
    drain_timeout_s: float,
) -> tuple[str, str]:
    try:
        return process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as original:
        observed, survivors, cleanup_confirmed = _stop_owned_launcher(job)
        stdout = _text(original.stdout)
        stderr = _text(original.stderr)
        try:
            drained_stdout, drained_stderr = process.communicate(timeout=drain_timeout_s)
            stdout = _merge_capture(stdout, drained_stdout)
            stderr = _merge_capture(stderr, drained_stderr)
        except subprocess.TimeoutExpired as drain:
            stdout = _merge_capture(stdout, drain.stdout)
            stderr = _merge_capture(stderr, drain.stderr)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        raise _LauncherTimeout(
            observed=observed,
            survivors=survivors,
            stdout=stdout,
            stderr=stderr,
            cleanup_confirmed=cleanup_confirmed,
        ) from original


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


def test_failed_post_cleanup_drain_preserves_original_partial_output(monkeypatch):
    process = Mock(spec=subprocess.Popen)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("launcher", 30, output="first-out", stderr="first-err"),
        subprocess.TimeoutExpired("launcher", 1, output="later-out", stderr=None),
    ]
    process.stdout = StringIO()
    process.stderr = StringIO()
    job = Mock(spec=windows_job.ProcessTreeJob)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_stop_owned_launcher",
        lambda _job: (("job active_processes=unknown",), ("unknown",), False),
    )

    with pytest.raises(_LauncherTimeout) as raised:
        _communicate_owned(
            cast(subprocess.Popen[str], process),
            cast(windows_job.ProcessTreeJob, job),
            timeout_s=30,
            drain_timeout_s=1,
        )

    assert raised.value.stdout == "first-outlater-out"
    assert raised.value.stderr == "first-err"
    assert raised.value.cleanup_confirmed is False
    assert raised.value.survivors == ("unknown",)
    assert process.stdout.closed
    assert process.stderr.closed


def test_process_identity_rejects_reused_pid(monkeypatch):
    process = Mock(spec=psutil.Process)
    process.create_time.return_value = 200.0
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)

    assert _process_identity_exists(1234, 100.0) is False


def test_process_identity_keeps_unknown_observation_live(monkeypatch):
    process = Mock(spec=psutil.Process)
    process.create_time.side_effect = psutil.AccessDenied(pid=1234)
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)

    assert _process_identity_exists(1234, 100.0) is True


def test_zero_job_count_cannot_confirm_known_descendant_containment():
    with pytest.raises(AssertionError, match="active_processes=0"):
        _require_accounted_job_process(("job active_processes=0",))


def test_positive_job_count_confirms_known_descendant_containment():
    assert _require_accounted_job_process(("job active_processes=1",)) == 1


def test_launcher_adoption_failure_kills_reaps_and_closes_captures(monkeypatch, tmp_path):
    process = Mock(spec=subprocess.Popen)
    process.pid = 1234
    process.stdout = StringIO()
    process.stderr = StringIO()
    job = Mock(spec=windows_job.ProcessTreeJob)
    job.adopt_suspended.side_effect = windows_job.JobContainmentError(5, "OpenProcess failed")
    monkeypatch.setattr(windows_job, "ProcessTreeJob", lambda: job)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(windows_job.JobContainmentError, match="OpenProcess"):
        _start_owned_launcher(["ignored"], cwd=tmp_path, env={})

    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)
    assert process.stdout.closed
    assert process.stderr.closed
    job.close.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Job Object regression")
def test_owned_capture_cleanup_survives_root_exit_with_inherited_handles(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    root = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "print('root-finished', flush=True)"
    )
    process, job = _start_owned_launcher(
        [sys.executable, "-c", root], cwd=tmp_path, env=dict(os.environ)
    )
    try:
        setup_deadline = time.monotonic() + 10
        while not child_pid_file.exists() and time.monotonic() < setup_deadline:
            time.sleep(0.02)
        assert child_pid_file.exists(), "launcher fixture did not publish its child PID"
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        child_create_time = psutil.Process(child_pid).create_time()
        assert process.wait(timeout=10) == 0, "launcher fixture root did not exit cleanly"

        started = time.monotonic()
        with pytest.raises(_LauncherTimeout) as raised:
            _communicate_owned(process, job, timeout_s=0.5, drain_timeout_s=1)
        elapsed = time.monotonic() - started

        _require_accounted_job_process(raised.value.observed)

        identity_deadline = time.monotonic() + 5
        while (
            _process_identity_exists(child_pid, child_create_time)
            and time.monotonic() < identity_deadline
        ):
            time.sleep(0.02)
        assert not _process_identity_exists(child_pid, child_create_time), (
            "original child identity remained after confirmed cleanup: "
            f"pid={child_pid}, create_time={child_create_time}"
        )
    finally:
        job.close()

    assert elapsed < 5
    assert raised.value.cleanup_confirmed is True
    assert raised.value.survivors == ()
    assert "root-finished" in raised.value.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows PowerShell acceptance")
def test_windows_public_launcher_preflights_real_product_help():
    env = dict(os.environ)
    env["LCA_STARTUP_TRACE"] = "1"
    process, job = _start_owned_launcher(
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
    )
    try:
        stdout, stderr = _communicate_owned(
            process, job, timeout_s=30, drain_timeout_s=10
        )
    except _LauncherTimeout as exc:
        pytest.fail(
            "public launcher exceeded 30s; owned descendants before cleanup: "
            f"{exc.observed or ('none observed',)}\n"
            f"cleanup confirmed: {exc.cleanup_confirmed}\n"
            f"descendants surviving cleanup: {exc.survivors or ('none',)}\n"
            f"stdout before timeout:\n{exc.stdout}\n"
            f"stderr/startup trace before timeout:\n{exc.stderr}"
        )
    finally:
        job.close()

    assert process.returncode == 0, stdout + stderr
    assert "Local Code Agent runtime preflight failed" not in stdout + stderr
    assert "stage=interpreter-selected" in stderr
    assert "stage=preflight-finished detail=exit=0" in stderr
    assert "stage=help-finished detail=exit=0" in stderr
