"""Linux process-tree supervisor used by :mod:`process_runner`.

Process groups are signalling scopes, not ownership scopes: a descendant can leave
one with ``setsid()``.  This helper becomes a Linux child subreaper before it starts
the requested command.  Orphans therefore return here even after changing session.
The helper does not exit until the command has exited and every remaining child has
been killed and reaped, and reports that kernel-observed result over a private pipe.

It runs once per owned command, so its start-up is on every command's critical path
(#455). The runner starts it with ``python -I -S``, and it imports only the few
standard modules it needs: no site packages, dataclasses, pathlib, json or typing.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_DEFAULT_DRAIN_SECONDS = 5.0
_POLL_SECONDS = 0.01

# POSIX-only calls are looked up through vars(os) so the module still imports (for its
# tests) and type-checks where they do not exist; the supervisor only runs on Linux.
_waitpid = vars(os)["waitpid"]
_wnohang: int = getattr(os, "WNOHANG", 1)
_sigkill: int = getattr(signal, "SIGKILL", 9)


class _Flags:
    """Supervisor state the SIGTERM/SIGINT handler reads and writes."""

    stop_requested = False
    # True only while the supervisor blocks in waitpid on its leader, so Stop breaks
    # that wait instead of the supervisor polling for it.
    in_leader_wait = False


class _LeaderWaitInterruptedError(Exception):
    """Stop arrived while the supervisor was blocked waiting for its leader."""


def _request_stop(_signum: int, _frame: object) -> None:
    _Flags.stop_requested = True
    if _Flags.in_leader_wait:
        raise _LeaderWaitInterruptedError


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
    depth = len(_namespace_pids("/proc/self/status"))
    found: list[int] = []
    for host_pid in _procfs_children():
        try:
            found.append(_namespace_pids(f"/proc/{host_pid}/status")[depth - 1])
        except (OSError, ValueError, IndexError, StopIteration):
            continue          # exited meanwhile; waitpid is the authority anyway
    return tuple(found)


def _procfs_children() -> tuple[int, ...]:
    """Direct children as procfs PIDs.

    Read from the kernel's own per-task child lists when it keeps them
    (CONFIG_PROC_CHILDREN), otherwise by scanning every process's parent. Either is a
    signal-target list only; no third-party process library is loaded (#455).
    """
    pids: list[int] = []
    try:
        for task in os.listdir("/proc/self/task"):
            pids.extend(int(pid) for pid in _read(f"/proc/self/task/{task}/children").split())
    except FileNotFoundError:
        pass
    else:
        return tuple(pids)
    parent = _host_pid("/proc/self/status")
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = _read(f"/proc/{name}/stat")
            # Field 4 (ppid) follows the parenthesised command name, which may itself
            # contain spaces and parentheses.
            if int(stat.rsplit(")", 1)[1].split()[1]) == parent:
                pids.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return tuple(pids)


def _read(path: str) -> str:
    with open(path, encoding="ascii", errors="replace") as stream:
        return stream.read()


def _host_pid(status_path: str) -> int:
    status = _read(status_path)
    return int(next(line.split()[1] for line in status.splitlines() if line.startswith("Pid:")))


def _namespace_pids(status_path: str) -> tuple[int, ...]:
    """``NSpid``: the process's PID at each level, from procfs's namespace inward.

    The supervisor signals in its own namespace, at its own depth. A child that
    created a nested PID namespace (``unshare --pid --fork``) has more levels, and
    its innermost PID (usually 1) names a different process here.
    """
    status = _read(status_path)
    line = next(line for line in status.splitlines() if line.startswith("NSpid:"))
    return tuple(int(field) for field in line.split()[1:])


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


def _report_line(
    leader_exit_code: int | None, stray_descendants: int, *, cleanup_confirmed: bool,
    setup_error: str | None = None,
) -> bytes:
    """The one-line JSON report the runner validates (``_supervisor_report``)."""
    if setup_error is None:
        error = "null"
    else:
        import json  # only a failed setup has text to escape
        error = json.dumps(setup_error)
    code = "null" if leader_exit_code is None else str(int(leader_exit_code))
    confirmed = "true" if cleanup_confirmed else "false"
    return (
        f'{{"cleanup_confirmed": {confirmed}, "leader_exit_code": {code}, '
        f'"setup_error": {error}, "stray_descendants": {int(stray_descendants)}}}\n'
    ).encode()


def _write_report(fd: int, payload: bytes) -> None:
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
    report_fd_text = os.environ.pop("LCA_POSIX_SUPERVISOR_FD", "")
    if not report_fd_text:
        return 125
    report_fd = int(report_fd_text)
    drain_s = _DEFAULT_DRAIN_SECONDS
    if args[:1] == ["--drain-s"]:
        # The runner owns the budget: it waits for this drain plus a margin before it
        # may end the supervisor, so the two bounds are one contract.
        try:
            drain_s = float(args[1])
        except (IndexError, ValueError):
            return _report_setup_error(report_fd, "invalid drain budget")
        if not 0 < drain_s < float("inf"):
            return _report_setup_error(report_fd, "invalid drain budget")
        args = args[2:]
    if args[:1] == ["--"]:
        args.pop(0)
    if not args:
        return _report_setup_error(report_fd, "empty command")
    return _supervise(args, report_fd, drain_s)


def _report_setup_error(report_fd: int, message: str) -> int:
    _write_report(report_fd, _report_line(None, 0, cleanup_confirmed=False,
                                          setup_error=message))
    return 125


def _start_leader(args: list[str], report_fd: int) -> int | None:
    """Fork the leader held at a pre-exec gate, verify it is a direct child, release it.

    The gate lives in the forked child itself rather than in a second interpreter:
    no requested command instruction runs before the supervisor has seen the leader
    among its children and released it (#455: one interpreter per run, not two).
    """
    gate_read, gate_write = os.pipe()
    try:
        leader: int = vars(os)["fork"]()
    except OSError as exc:
        os.close(gate_read)
        os.close(gate_write)
        _report_setup_error(report_fd, str(exc))
        return None
    if leader == 0:
        _exec_when_released(args, gate_read, gate_write)
    os.close(gate_read)
    if leader not in _children():
        os.close(gate_write)
        os.kill(leader, _sigkill)
        _waitpid(leader, 0)
        _report_setup_error(report_fd, "direct-child accounting is unavailable")
        return None
    os.write(gate_write, b"go\n")
    os.close(gate_write)
    return leader


def _supervise(args: list[str], report_fd: int, drain_s: float) -> int:
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
    try:
        _Flags.in_leader_wait = True
        if not _Flags.stop_requested:
            # Blocks until the leader exits; Stop raises out of it (see _request_stop).
            _pid, leader_status = _waitpid(leader, 0)
    except (_LeaderWaitInterruptedError, ChildProcessError):
        pass
    finally:
        _Flags.in_leader_wait = False

    deadline = time.monotonic() + drain_s
    if leader_status is None:
        # Stop/timeout: the leader is still a child and is included in the drain.
        stray_count, confirmed = _drain_children(deadline)
        leader_code = None
    else:
        leader_code = _exit_status(leader_status)
        stray_count, confirmed = _drain_children(deadline)
    _write_report(report_fd, _report_line(leader_code, stray_count,
                                          cleanup_confirmed=confirmed))
    if leader_code is None:
        return 143
    return leader_code


def _exec_when_released(args: list[str], gate_read: int, gate_write: int) -> None:
    """In the forked leader: wait at the gate, then become the requested command.

    Only the forked child runs this, and it never returns (annotated ``None`` only to
    keep ``typing`` off the start-up path): it execs, or ends the child with a
    shell-style status (125 not released, 127 exec failed).
    """
    try:
        os.close(gate_write)
        vars(os)["setsid"]()
        # exec keeps ignored dispositions, and Python ignores SIGPIPE and SIGXFSZ;
        # restore what any freshly started program expects, as subprocess does.
        for name in ("SIGPIPE", "SIGXFSZ", "SIGTERM", "SIGINT"):
            number = vars(signal).get(name)
            if number is not None:
                signal.signal(number, signal.SIG_DFL)
        released = os.read(gate_read, 3)
        if released != b"go\n":
            os._exit(125)
        os.closerange(3, vars(os)["sysconf"]("SC_OPEN_MAX"))
        os.execvp(args[0], args)  # noqa: S606 - argv resolved and admitted by the runner
    except BaseException:  # noqa: BLE001 - a forked child must never return
        os._exit(127)


if __name__ == "__main__":
    raise SystemExit(main())
