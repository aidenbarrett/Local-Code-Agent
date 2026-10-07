"""Windows Job Object containment for configured-command process trees.

A process is created suspended, assigned to a fresh Job Object and only then resumed,
so no instruction of the child runs outside the job. Descendants inherit the job and
cannot leave it: the job never grants ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` or
``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``, so ``CREATE_BREAKAWAY_FROM_JOB`` fails in the
descendant. ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` means the tree also dies if this
controller dies without running its own cleanup.

Termination is a claim only when every process the job held is proved terminated.
``TerminateJobObject`` returning success is a request, not that proof, and neither is
the job's ``ActiveProcesses`` count: Windows CI observed it at zero while a member's
process handle was still unsignaled (#445). The proof is each member's own process
handle becoming signaled, plus a complete, empty final membership.

This module is inert on non-Windows platforms. The structure layouts are defined
everywhere so their sizes can be checked on any 64-bit host.
"""
from __future__ import annotations

import ctypes
import contextlib
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS = 3
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SUSPEND_RESUME = 0x0800
_SYNCHRONIZE = 0x00100000

_ERROR_INVALID_PARAMETER = 87
_ERROR_MORE_DATA = 234
_WAIT_OBJECT_0 = 0x00000000
_MAXIMUM_WAIT_OBJECTS = 64
_PROCESS_ID_LIST_ATTEMPTS = 8

CREATE_SUSPENDED = 0x00000004

_TERMINATED_EXIT_CODE = 1
_DRAIN_POLL_S = 0.02


def terminate_unadopted(process: subprocess.Popen[Any], timeout_s: float) -> bool:
    """End a direct suspended child that no Job Object has adopted yet.

    The caller retains the ``Popen`` authority until adoption succeeds.  In
    particular, an ``OpenProcess`` refusal leaves the Job Object unable to reach the
    child, so closing that empty job is not cleanup.
    """
    with contextlib.suppress(OSError):
        process.kill()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
    return True


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


def _process_id_list_type(capacity: int) -> type[ctypes.Structure]:
    """``JOBOBJECT_BASIC_PROCESS_ID_LIST`` with room for ``capacity`` ids."""

    class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):  # noqa: N801 - Win32 name
        _fields_ = [
            ("NumberOfAssignedProcesses", ctypes.c_uint32),
            ("NumberOfProcessIdsInList", ctypes.c_uint32),
            ("ProcessIdList", ctypes.c_size_t * max(capacity, 1)),
        ]

    return JOBOBJECT_BASIC_PROCESS_ID_LIST


class JobContainmentError(OSError):
    """The OS refused a step that containment depends on."""


def _last_error() -> int:
    if sys.platform == "win32":
        return ctypes.get_last_error()
    else:
        raise JobContainmentError(0, "Windows Job Objects exist only on Windows")


def _raise_last_error(step: str) -> None:
    code = _last_error()
    raise JobContainmentError(code, f"{step} failed with Windows error {code}")


# The loaded DLLs are returned as Any on purpose: ctypes resolves exported functions by
# attribute at runtime, so no static type exists for them. Every function used is given
# explicit argtypes/restype below, which is where ctypes checks the calls.
def _load_dll(name: str, *, use_last_error: bool) -> Any:
    if sys.platform == "win32":
        return ctypes.WinDLL(name, use_last_error=use_last_error)
    else:
        raise JobContainmentError(0, "Windows Job Objects exist only on Windows")


def _kernel32() -> Any:
    kernel32 = _load_dll("kernel32", use_last_error=True)
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
    kernel32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
    return kernel32


def _ntdll() -> Any:
    ntdll = _load_dll("ntdll", use_last_error=False)
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

        After ``OpenProcess`` succeeds, assignment/resume failures terminate through
        that process handle.  Before then the caller still owns the suspended child's
        ``Popen`` and must terminate/reap it if adoption raises.
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
                code = _last_error()
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

    def member_pids(self) -> tuple[int, ...]:
        """A complete snapshot of the job's process ids; never a partial list.

        ``JOBOBJECT_BASIC_PROCESS_ID_LIST`` is variable-sized: when the list holds
        fewer ids than the job has processes, grow the buffer and ask again.
        """
        if self._handle is None:
            raise JobContainmentError(0, "job is closed")
        capacity = 16
        for _attempt in range(_PROCESS_ID_LIST_ATTEMPTS):
            info = _process_id_list_type(capacity)()
            ok = self._k32.QueryInformationJobObject(
                self._handle,
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            if not ok and _last_error() != _ERROR_MORE_DATA:
                _raise_last_error("QueryInformationJobObject(process id list)")
            assigned = int(info.NumberOfAssignedProcesses)
            listed = int(info.NumberOfProcessIdsInList)
            if ok and listed >= assigned:
                return tuple(int(info.ProcessIdList[i]) for i in range(listed))
            # Incomplete: the job grew past the buffer. Grow with headroom and retry.
            capacity = max(capacity * 2, assigned + 16)
        # A job that outgrows every retry cannot be snapshotted, so nothing it holds
        # can be proved ended.
        raise JobContainmentError(0, "job process id list never became complete")

    def terminate_and_confirm(self, timeout_s: float) -> bool:
        """Terminate every process in the job; True only when each one is proved ended.

        Proof is: each member seen (before or after termination) has a process
        handle that is signaled, or provably no longer exists; no member is left
        that was never seen; and the kernel's accounting agrees. A member that
        cannot be opened for any reason other than ``ERROR_INVALID_PARAMETER``
        (already gone) makes the result unconfirmed.

        One absolute deadline bounds the proof. It is checked after every step
        that can block (snapshot, opens, termination, waits, accounting), and the
        final verdict is only accepted if it was reached before the deadline:
        evidence completed after its bound certifies nothing. Every handle opened
        here is closed on every path.
        """
        if self._handle is None:
            return False
        deadline = time.monotonic() + timeout_s
        tracked: dict[int, int] = {}
        try:
            return self._prove_terminated(tracked, deadline)
        except JobContainmentError:
            return False
        finally:
            for handle in tracked.values():
                self._k32.CloseHandle(handle)

    def _prove_terminated(self, tracked: dict[int, int], deadline: float) -> bool:
        def late() -> bool:
            return time.monotonic() > deadline

        # Handles are opened while the processes are members, so a PID reused after
        # it exits cannot stand in for the member it was. A snapshot or an open that
        # finishes after the deadline is caught by the check that follows it.
        unconfirmable = self._track(self.member_pids(), tracked)
        if late():
            return False
        # A failure here (ERROR_ACCESS_DENIED can mean the job already emptied)
        # decides nothing: the member handles and later snapshots do.
        self._k32.TerminateJobObject(self._handle, _TERMINATED_EXIT_CODE)
        while not late() and self._wait_signaled(tuple(tracked.values()), deadline):
            # A member assigned after the first snapshot (a child spawned during
            # termination) must be seen and ended too; terminate again if so.
            fresh = tuple(pid for pid in self.member_pids() if pid not in tracked)
            if fresh:
                unconfirmable |= self._track(fresh, tracked)
                self._k32.TerminateJobObject(self._handle, _TERMINATED_EXIT_CODE)
                continue
            if unconfirmable or late():
                return False
            if self.active_processes() == 0:
                # Every member ever seen is signaled, no unseen member remains, and
                # the kernel's accounting agrees; accepted only inside the bound.
                return not late()
            time.sleep(min(_DRAIN_POLL_S, max(deadline - time.monotonic(), 0.0)))
        return False

    def _track(self, pids: tuple[int, ...], tracked: dict[int, int]) -> bool:
        """Open a wait handle for each new member. True if one cannot be proved."""
        unconfirmable = False
        for pid in pids:
            if pid in tracked:
                continue
            handle = self._k32.OpenProcess(_SYNCHRONIZE, False, pid)
            if handle:
                tracked[pid] = handle
            elif _last_error() != _ERROR_INVALID_PARAMETER:
                # Access denied or anything else: alive or not, it cannot be shown.
                unconfirmable = True
        return unconfirmable

    def _wait_signaled(self, handles: tuple[int, ...], deadline: float) -> bool:
        """Wait for every handle within the one deadline, in bounded batches."""
        for start in range(0, len(handles), _MAXIMUM_WAIT_OBJECTS):
            batch = handles[start:start + _MAXIMUM_WAIT_OBJECTS]
            remaining_ms = int(max(deadline - time.monotonic(), 0) * 1000)
            array = (wintypes.HANDLE * len(batch))(*batch)
            result = self._k32.WaitForMultipleObjects(len(batch), array, True, remaining_ms)
            if result != _WAIT_OBJECT_0:
                # Only WAIT_OBJECT_0 is taken as "all signaled". Anything else
                # (timeout, failure, abandoned, an index) is not proof.
                return False
        return True

    def close(self) -> None:
        """Close the job handle. KILL_ON_JOB_CLOSE ends anything still inside it."""
        handle, self._handle = self._handle, None
        if handle:
            self._k32.CloseHandle(handle)

    def __enter__(self) -> "ProcessTreeJob":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
