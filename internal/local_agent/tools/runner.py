"""Subprocess execution with owned process trees.

Every command runs with a fixed argv from repository config, cwd pinned to the
repository root, a timeout, and output captured to disk rather than the context
window. POSIX owns a process group. Windows creates the process suspended, assigns
it to a kill-on-close Job Object before it can spawn descendants, then resumes it.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass
class RunOutcome:
    command: list[str]
    exit_code: int
    elapsed_s: float
    timed_out: bool
    run_id: str
    stdout_path: Path
    stderr_path: Path
    combined_path: Path
    # None means no timeout cleanup was required. True means the owned process
    # container was terminated and observed empty before return. False means a
    # cleanup operation ran but could not establish that fact.
    process_cleanup_confirmed: bool | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def new_run_dir(run_root: Path) -> tuple[str, Path]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = run_root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return run_id, path


class _WindowsJob:
    """Minimal Win32 Job Object wrapper; instantiated only on Windows."""

    CREATE_SUSPENDED = 0x00000004
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicAccountingInformation = 1
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BASIC_ACCOUNTING(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ExtendedLimit = EXTENDED_LIMIT
        self._BasicAccounting = BASIC_ACCOUNTING

        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        self.handle = self._kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self.handle,
            self.JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def assign_and_resume(self, proc: subprocess.Popen[str]) -> None:
        handle = self._wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self.handle, handle):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")
        # CREATE_SUSPENDED prevents the process from racing a descendant outside
        # the job. Once assigned, every normal descendant inherits membership.
        psutil.Process(proc.pid).resume()

    def active_processes(self) -> int:
        info = self._BasicAccounting()
        if not self._kernel32.QueryInformationJobObject(
            self.handle,
            self.JobObjectBasicAccountingInformation,
            self._ctypes.byref(info),
            self._ctypes.sizeof(info),
            None,
        ):
            raise OSError(self._ctypes.get_last_error(), "QueryInformationJobObject failed")
        return int(info.ActiveProcesses)

    def terminate_and_wait_empty(self, timeout: float = 5.0) -> bool:
        if not self._kernel32.TerminateJobObject(self.handle, 1):
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.active_processes() == 0:
                    return True
            except OSError:
                return False
            time.sleep(0.02)
        return False

    def close(self) -> None:
        handle = getattr(self, "handle", None)
        if handle:
            self._kernel32.CloseHandle(handle)
            self.handle = None


def _kill_timed_out_process(
    proc: subprocess.Popen[str], windows_job: _WindowsJob | None = None
) -> bool:
    if os.name == "nt":
        if windows_job is None:
            return False
        return windows_job.terminate_and_wait_empty()

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def run_command(
    command: list[str],
    cwd: Path,
    run_root: Path,
    timeout_s: int,
    env_overrides: dict[str, str] | None = None,
) -> RunOutcome:
    if not command:
        raise ValueError("empty command")

    from .base import BlockedError, Reason

    exe = shutil.which(command[0])
    if exe is None:
        raise BlockedError(
            f"{command[0]!r} is not on PATH. Fix the build profile, do not ask "
            "the model to improvise.",
            Reason.MISSING_EXECUTABLE,
        )

    run_id, run_dir = new_run_dir(run_root)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    combined_path = run_dir / "combined.log"

    env = dict(os.environ)
    env.update(env_overrides or {})
    env.setdefault("CLICOLOR", "0")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("GIT_PAGER", "cat")

    started = time.monotonic()
    timed_out = False
    cleanup_confirmed: bool | None = None
    windows_job: _WindowsJob | None = None
    proc: subprocess.Popen[str] | None = None
    try:
        if os.name == "nt":
            try:
                windows_job = _WindowsJob()
            except OSError as exc:
                raise BlockedError(
                    f"could not create a Windows process-containment job: {exc}",
                    Reason.SPAWN_FAILURE,
                ) from exc

        proc = subprocess.Popen(
            [exe, *command[1:]],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=(os.name != "nt"),
            creationflags=(_WindowsJob.CREATE_SUSPENDED if os.name == "nt" else 0),
        )
        if windows_job is not None:
            try:
                windows_job.assign_and_resume(proc)
            except (OSError, psutil.Error) as exc:
                # Never continue a tool after containment failed. Kill the
                # suspended process through the job if assignment succeeded, or
                # directly as a fallback, then fail closed.
                try:
                    windows_job.terminate_and_wait_empty()
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
                raise BlockedError(
                    f"could not establish Windows process-tree containment: {exc}",
                    Reason.SPAWN_FAILURE,
                ) from exc

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            code = int(proc.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_confirmed = _kill_timed_out_process(proc, windows_job)
            stdout, stderr = proc.communicate()
            stderr += (
                f"\n[local-agent] command exceeded {timeout_s}s; "
                f"process-tree cleanup confirmed={str(cleanup_confirmed).lower()}\n"
            )
            code = 124
    except OSError as exc:
        raise BlockedError(
            f"could not start {exe!r}: {exc.strerror or exc}", Reason.SPAWN_FAILURE
        ) from exc
    finally:
        # KILL_ON_JOB_CLOSE also prevents a nominally successful tool from leaving
        # detached descendants behind after the owning command exits.
        if windows_job is not None:
            windows_job.close()

    elapsed = time.monotonic() - started
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined_path.write_text(stdout + stderr, encoding="utf-8")
    (run_dir / "command.txt").write_text(
        " ".join(command)
        + f"\nexit={code} elapsed={elapsed:.2f}s timed_out={str(timed_out).lower()} "
        + f"cleanup_confirmed={cleanup_confirmed}\n",
        encoding="utf-8",
    )

    return RunOutcome(
        command=command,
        exit_code=code,
        elapsed_s=elapsed,
        timed_out=timed_out,
        run_id=run_id,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        combined_path=combined_path,
        process_cleanup_confirmed=cleanup_confirmed,
    )
