"""Linux process-tree supervisor used by :mod:`process_runner`.

Process groups are signalling scopes, not ownership scopes: a descendant can leave
one with ``setsid()``.  This helper becomes a Linux child subreaper before it starts
the requested command.  Orphans therefore return here even after changing session.
The helper does not exit until the command has exited and every remaining child has
been killed and reaped, and reports that kernel-observed result over a private pipe.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable
from typing import cast

import psutil


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_DRAIN_SECONDS = 10.0
_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class SupervisorReport:
    leader_exit_code: int | None
    stray_descendants: int
    cleanup_confirmed: bool
    setup_error: str | None = None


_WaitPid = Callable[[int, int], tuple[int, int]]
_waitpid = cast(_WaitPid, vars(os)["waitpid"])
_wnohang = cast(int, vars(os)["WNOHANG"])
_sigkill = cast(int, vars(signal).get("SIGKILL", 9))
_stop_requested = threading.Event()


def _request_stop(_signum: int, _frame: object) -> None:
    _stop_requested.set()


def _enable_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    observed = ctypes.c_int(0)
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(observed), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if observed.value != 1:
        raise RuntimeError("the kernel did not retain child-subreaper ownership")


def _children() -> tuple[int, ...]:
    """Current direct children, used only as signal targets.

    Enumeration is never proof of emptiness.  Only ``waitpid`` returning ``ECHILD``
    after subreaper adoption supplies that proof.
    """
    host_parent = _host_pid(Path("/proc/self/status"))
    found: list[int] = []
    for candidate in psutil.process_iter(("pid", "ppid")):
        try:
            if int(candidate.info["ppid"]) != host_parent:
                continue
            host_pid = int(candidate.info["pid"])
            found.append(_namespace_pid(Path(f"/proc/{host_pid}/status")))
        except (KeyError, TypeError, ValueError, OSError, psutil.Error):
            continue
    return tuple(found)


def _host_pid(status_path: Path) -> int:
    status = status_path.read_text(encoding="ascii")
    return int(next(line.split()[1] for line in status.splitlines() if line.startswith("Pid:")))


def _namespace_pid(status_path: Path) -> int:
    status = status_path.read_text(encoding="ascii")
    return int(next(line.split()[-1] for line in status.splitlines()
                    if line.startswith("NSpid:")))


def _reap_exited() -> None:
    while True:
        try:
            pid, _status = _waitpid(-1, _wnohang)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _drain_children(deadline: float) -> tuple[int, bool]:
    """Kill descendants from the root down until the kernel reports no children."""
    seen: set[int] = set()
    while True:
        _reap_exited()
        children = _children()
        if not children:
            try:
                pid, _status = _waitpid(-1, _wnohang)
            except ChildProcessError:
                return len(seen), True
            if pid == 0:
                # procfs and waitpid disagree transiently; keep the result unknown.
                if time.monotonic() >= deadline:
                    return len(seen), False
                time.sleep(_POLL_SECONDS)
                continue
            continue
        seen.update(children)
        for pid in children:
            try:
                os.kill(pid, _sigkill)
            except ProcessLookupError:
                pass
            except PermissionError:
                return len(seen), False
        if time.monotonic() >= deadline:
            return len(seen), False
        time.sleep(_POLL_SECONDS)


def _write_report(fd: int, report: SupervisorReport) -> None:
    payload = json.dumps(asdict(report), sort_keys=True).encode("utf-8") + b"\n"
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.close(fd)


def _exit_status(status: int) -> int:
    signal_number = status & 0x7F
    return status >> 8 if signal_number == 0 else 128 + signal_number


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--command-gate"]:
        return _command_gate(args[1:])
    if args[:1] == ["--"]:
        args.pop(0)
    report_fd_text = os.environ.pop("LCA_POSIX_SUPERVISOR_FD", "")
    if not report_fd_text:
        return 125
    report_fd = int(report_fd_text)
    if not args:
        return _report_setup_error(report_fd, "empty command")
    return _supervise(args, report_fd)


def _report_setup_error(report_fd: int, message: str) -> int:
    _write_report(report_fd, SupervisorReport(None, 0, False, message))
    return 125


def _start_leader(args: list[str], report_fd: int) -> subprocess.Popen[bytes] | None:
    gate_read, gate_write = os.pipe()
    try:
        leader = subprocess.Popen(  # noqa: S603 - fixed Python gate, fixed argv
            [sys.executable, __file__, "--command-gate", str(gate_read), "--", *args],
            close_fds=True,
            pass_fds=(gate_read,),
            start_new_session=True,
        )
    except OSError as exc:
        os.close(gate_read)
        os.close(gate_write)
        _report_setup_error(report_fd, str(exc))
        return None
    os.close(gate_read)
    if leader.pid not in _children():
        os.close(gate_write)
        leader.kill()
        leader.wait()
        _report_setup_error(report_fd, "direct-child accounting is unavailable")
        return None
    os.write(gate_write, b"go\n")
    os.close(gate_write)
    return leader


def _supervise(args: list[str], report_fd: int) -> int:
    try:
        _enable_subreaper()
    except (OSError, RuntimeError) as exc:
        return _report_setup_error(report_fd, str(exc))

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    leader = _start_leader(args, report_fd)
    if leader is None:
        return 125

    leader_status: int | None = None
    while leader_status is None and not _stop_requested.is_set():
        try:
            pid, status = _waitpid(leader.pid, _wnohang)
        except ChildProcessError:
            break
        if pid == leader.pid:
            leader_status = status
            break
        time.sleep(_POLL_SECONDS)

    deadline = time.monotonic() + _DRAIN_SECONDS
    if leader_status is None:
        # Stop/timeout: the leader is still a child and is included in the drain.
        stray_count, confirmed = _drain_children(deadline)
        leader_code = None
    else:
        leader_code = _exit_status(leader_status)
        stray_count, confirmed = _drain_children(deadline)
    _write_report(
        report_fd,
        SupervisorReport(leader_code, stray_count, confirmed),
    )
    if leader_code is None:
        return 143
    return leader_code


def _command_gate(args: list[str]) -> int:
    """Child-side pre-exec gate: no requested command instruction runs before release."""
    if len(args) < 3 or args[1] != "--":
        return 125
    gate_fd = int(args[0])
    try:
        released = os.read(gate_fd, 3)
    finally:
        os.close(gate_fd)
    if released != b"go\n":
        return 125
    command = args[2:]
    os.execvpe(command[0], command, os.environ)  # noqa: S606 - resolved by the owner


if __name__ == "__main__":
    raise SystemExit(main())
