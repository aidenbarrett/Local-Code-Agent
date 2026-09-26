"""Windows Job Object containment for configured-command process trees.

A process is created suspended, assigned to a fresh Job Object and only then resumed,
so no instruction of the child runs outside the job. Descendants inherit the job and
cannot leave it: the job never grants ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` or
``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``, so ``CREATE_BREAKAWAY_FROM_JOB`` fails in the
descendant. ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` means the tree also dies if this
controller dies without running its own cleanup.

Termination is a claim only after the kernel's own accounting says the job holds no
active process. ``TerminateJobObject`` returning success is a request, not that proof.

This module is inert on non-Windows platforms. The structure layouts are defined
everywhere so their sizes can be checked on any 64-bit host.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SUSPEND_RESUME = 0x0800

CREATE_SUSPENDED = 0x00000004

_TERMINATED_EXIT_CODE = 1
_DRAIN_POLL_S = 0.02


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class JobContainmentError(OSError):
    """The OS refused a step that containment depends on."""


def _raise_last_error(step: str) -> None:
    code = ctypes.get_last_error()  # type: ignore[attr-defined]
    raise JobContainmentError(code, f"{step} failed with Windows error {code}")


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _ntdll():
    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
    # NtResumeProcess resumes every thread of a process. subprocess.Popen closes the
    # primary thread handle, so this is the only way to resume a CREATE_SUSPENDED
    # child without enumerating threads.
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return ntdll


def supported() -> bool:
    return os.name == "nt"


class ProcessTreeJob:
    """One kill-on-close Job Object owning exactly one spawned command tree."""

    def __init__(self) -> None:
        if not supported():
            raise JobContainmentError(0, "Job Objects exist only on Windows")
        self._k32 = _kernel32()
        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            _raise_last_error("CreateJobObjectW")
        self._handle: int | None = handle
        try:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self._k32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _raise_last_error("SetInformationJobObject")
        except BaseException:
            self.close()
            raise

    def adopt_suspended(self, pid: int) -> None:
        """Assign a CREATE_SUSPENDED process to this job, then resume it.

        On any failure the suspended process is terminated before the error escapes, so a
        refused adoption can never leave an uncontained child running or a suspended
        child stranded.
        """
        if self._handle is None:
            raise JobContainmentError(0, "job is closed")
        access = (
            _PROCESS_SET_QUOTA
            | _PROCESS_TERMINATE
            | _PROCESS_SUSPEND_RESUME
            | _PROCESS_QUERY_LIMITED_INFORMATION
        )
        process = self._k32.OpenProcess(access, False, pid)
        if not process:
            _raise_last_error("OpenProcess")
        try:
            if not self._k32.AssignProcessToJobObject(self._handle, process):
                code = ctypes.get_last_error()  # type: ignore[attr-defined]
                self._k32.TerminateProcess(process, _TERMINATED_EXIT_CODE)
                raise JobContainmentError(
                    code, f"AssignProcessToJobObject failed with Windows error {code}"
                )
            status = _ntdll().NtResumeProcess(process)
            if status < 0:
                self._k32.TerminateProcess(process, _TERMINATED_EXIT_CODE)
                raise JobContainmentError(
                    status, f"NtResumeProcess failed with NTSTATUS {status & 0xFFFFFFFF:#010x}"
                )
        finally:
            self._k32.CloseHandle(process)

    def active_processes(self) -> int:
        if self._handle is None:
            raise JobContainmentError(0, "job is closed")
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not self._k32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            _raise_last_error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def terminate_and_confirm(self, timeout_s: float) -> bool:
        """Terminate every process in the job; True only if accounting reaches zero."""
        if self._handle is None:
            return False
        try:
            if not self._k32.TerminateJobObject(self._handle, _TERMINATED_EXIT_CODE):
                # ERROR_ACCESS_DENIED here can mean the job already emptied. Accounting
                # below is the only thing allowed to decide.
                pass
            deadline = time.monotonic() + timeout_s
            while True:
                if self.active_processes() == 0:
                    return True
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_DRAIN_POLL_S)
        except JobContainmentError:
            return False

    def close(self) -> None:
        """Close the job handle. KILL_ON_JOB_CLOSE ends anything still inside it."""
        handle, self._handle = self._handle, None
        if handle:
            self._k32.CloseHandle(handle)

    def __enter__(self) -> "ProcessTreeJob":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
