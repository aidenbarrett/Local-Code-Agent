"""Subprocess execution.

Every command runs with a fixed argv from the repository config, a working
directory pinned to the repository root, a timeout and a hard output bound. Its
output is written to the run directory as evidence, never into the context window.

Timeout and cancellation are execution-state claims, not just return codes. A POSIX
session gives us a useful process group to kill, but it is not containment: a descendant
can call ``setsid()`` and escape that group, so POSIX cleanup is reported unconfirmed.

On Windows the command is created suspended and adopted by a kill-on-close Job Object
before its first instruction runs (``windows_job``). Descendants cannot break away, and
cleanup is confirmed only when the kernel's job accounting reports zero active
processes. If the OS refuses the job, the runner falls back to visible-tree enumeration
and reports cleanup unconfirmed, exactly as before.

A normally exiting command must not leave an owned process behind on either platform.
Descendants still inside the job (Windows) or the session's process group (POSIX) when
the direct child exits are given a short settle window to finish on their own, then
counted, ended and drained within a bound. If they cannot be shown gone, the run says so
(``strays_unconfirmed``) and callers that own effects fail the step.

Output is bounded while the command runs, not only after it exits: stdout and stderr
together may not exceed the caller's limit, checked on every poll, and crossing it ends
the owned tree before ``CommandOutputLimitError`` is raised. Growth past the limit is
bounded by one poll interval of writing.

Captured output never lives in a public run directory. On Windows it goes to private
temporary files, which no writer can grow after the run because the job ends every
descendant. On POSIX it goes to pipes the controller drains under the byte limit and
closes when the run ends: a descendant that escaped the process group with setsid()
then gets EPIPE, not disk. A POSIX process group is not a process tree, and nothing
here claims it is: ``containment="process_group"`` and cleanup stays unconfirmed.

An ``on_spawn`` callback runs before the command can have any effect: the child is
suspended on Windows and held at a shell gate on POSIX until the callback returns.
"""

from __future__ import annotations

import contextlib
import mmap
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import IO, Protocol

import psutil

from . import windows_job


_POST_KILL_WAIT_S = 2.0
# Descendants a normally exiting command abandoned: long enough for a loaded machine's
# kernel accounting to drain, and bounded.
_STRAY_DRAIN_S = 10.0
_POST_KILL_FORCE_WAIT_S = 1.0
_CANCEL_POLL_S = 0.05
# After the direct child exits, how long its tree may take to finish on its own before
# anything left is counted as abandoned. Windows job accounting can lag the exit of the
# direct child itself by a few milliseconds (#435); a real stray will not finish here.
_STRAY_SETTLE_S = 0.25


class CommandCancellationRequested(RuntimeError):
    """Cancellation was already requested before a command side effect began."""


class CancellationProbe(Protocol):
    @property
    def requested(self) -> bool: ...


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
    # None means no cleanup was required. False means cleanup ran but the runner cannot
    # prove the whole tree ended. True means the tree's owner (a Windows Job Object)
    # accounted for every process ending: after a timeout or Stop, or after a normally
    # exiting command left descendants behind in its job.
    process_cleanup_confirmed: bool | None = None
    cancel_requested: bool = False
    # Whole-tree ownership held for this run: "job_object" (Windows Job Object),
    # "process_group" (POSIX session, escapable) or "visible_tree" (Windows fallback).
    containment: str = "process_group"
    # Descendants still inside the job (Windows) or the session's process group (POSIX)
    # when the direct child exited normally, which the runner then ended. None when the
    # containment cannot count them (Windows visible-tree fallback, or no normal exit).
    stray_descendants_at_exit: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cancel_requested


def new_run_dir(run_root: Path) -> tuple[str, Path]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = run_root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return run_id, path


def _best_effort_windows_tree_kill(proc: subprocess.Popen[bytes]) -> None:
    """Reconcile the visible tree, without claiming Job-Object containment."""
    try:
        parent = psutil.Process(proc.pid)
        descendants = parent.children(recursive=True)
    except psutil.Error:
        descendants = []
        parent = None

    targets = descendants + ([parent] if parent is not None else [])
    # Each step is best effort by design: a process may exit between enumeration and
    # signal. The caller never treats this path as proof of cleanup.
    for target in reversed(targets):
        with contextlib.suppress(psutil.Error):
            target.terminate()
    _, alive = psutil.wait_procs(targets, timeout=1.0)
    for target in alive:
        with contextlib.suppress(psutil.Error):
            target.kill()
    if alive:
        psutil.wait_procs(alive, timeout=1.0)
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()


def _kill_process_tree_best_effort(
    proc: subprocess.Popen[bytes], job: windows_job.ProcessTreeJob | None = None
) -> bool:
    """Best-effort cleanup; return whether whole-tree cleanup is proven."""
    if job is not None:
        confirmed = job.terminate_and_confirm(_POST_KILL_WAIT_S)
        if not confirmed:
            # The kernel still reports live members. Try the visible tree too, but the
            # answer stays unconfirmed: only job accounting may say the tree is gone.
            _best_effort_windows_tree_kill(proc)
        return confirmed
    if sys.platform == "win32":
        _best_effort_windows_tree_kill(proc)
        # Enumeration is not containment. A Windows Job Object is required before
        # this runner may return True here.
        return False
    else:
        # The group may already be gone; that is not a cleanup failure.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        # start_new_session=True makes the direct child the process-group leader, so
        # killpg reliably kills processes that stayed in that group. It cannot prove
        # that a descendant did not call setsid()/setpgid() first and escape. A cgroup
        # (or equivalent containment primitive) is required before POSIX may return
        # True here.
        return False


def _bounded_reap(proc: subprocess.Popen[bytes]) -> None:
    """Reap the direct child without ever turning cleanup into a hang."""
    try:
        proc.wait(timeout=_POST_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass
    else:
        return

    with contextlib.suppress(OSError):
        proc.kill()
    # The timeout/cancellation result remains fail-closed. Returning is preferable to
    # hanging the controller while pretending the process tree was owned.
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_POST_KILL_FORCE_WAIT_S)


def _probe_requested(probe: CancellationProbe | None) -> bool:
    if probe is None:
        return False
    requested = probe.requested
    if not isinstance(requested, bool):
        raise TypeError("cancellation probe requested state must be boolean")
    return requested


class CommandSpawnError(RuntimeError):
    """The command could not be started; nothing ran."""

    def __init__(self, message: str, *, missing_executable: bool) -> None:
        super().__init__(message)
        self.missing_executable = missing_executable


class CommandOutputLimitError(RuntimeError):
    """The command wrote more than the caller agreed to read; its output is not evidence.

    Raised only after the owned tree was ended. ``cleanup_confirmed`` and
    ``containment`` carry the same meaning as on :class:`OwnedRun`.
    """

    def __init__(
        self, message: str, *, cleanup_confirmed: bool | None, containment: str,
    ) -> None:
        super().__init__(message)
        self.cleanup_confirmed = cleanup_confirmed
        self.containment = containment


@dataclass(frozen=True)
class OwnedRun:
    """One owned process tree's result, with its output in memory."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    elapsed_s: float
    timed_out: bool
    cancel_requested: bool
    # Same meaning as RunOutcome.process_cleanup_confirmed.
    cleanup_confirmed: bool | None
    containment: str
    stray_descendants_at_exit: int | None
    # Descendants outlived a normally exiting command and their end was not confirmed.
    strays_unconfirmed: bool


_PIPE_READ_CHUNK = 1 << 16


class _Captures(Protocol):
    """Where a run's stdout and stderr go, and the aggregate byte limit they share."""

    limit: int | None

    def pump(self, proc: subprocess.Popen[bytes], wait_s: float) -> None:
        """Take in output written so far, waiting up to ``wait_s`` for output or exit."""

    def total(self) -> int: ...

    def finish(self) -> tuple[bytes, bytes]:
        """Final output, after which nothing a leftover process writes is accepted."""


def _over_limit(captures: _Captures) -> int | None:
    """Bytes written so far when past the limit, else None."""
    if captures.limit is None:
        return None
    total = captures.total()
    return total if total > captures.limit else None


class _FileCaptures:
    """Private temporary files (Windows). The Job Object ends every descendant before
    the owner returns, so no writer can grow them afterwards; only the visible-tree
    fallback cannot promise that, and it already reports cleanup unconfirmed."""

    def __init__(self, stdout: IO[bytes], stderr: IO[bytes], limit: int | None) -> None:
        self.stdout, self.stderr, self.limit = stdout, stderr, limit

    def pump(self, proc: subprocess.Popen[bytes], wait_s: float) -> None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=wait_s)

    def total(self) -> int:
        return os.fstat(self.stdout.fileno()).st_size + os.fstat(self.stderr.fileno()).st_size

    def finish(self) -> tuple[bytes, bytes]:
        return self._snapshot(self.stdout), self._snapshot(self.stderr)

    @staticmethod
    def _snapshot(capture: IO[bytes]) -> bytes:
        """Read a fixed-length snapshot without moving an inherited file offset."""
        size = os.fstat(capture.fileno()).st_size
        if size == 0:
            return b""
        with mmap.mmap(capture.fileno(), length=size, access=mmap.ACCESS_READ) as view:
            return view[:]


if sys.platform != "win32":

    class _PipeCaptures:
        """Controller-drained pipes (POSIX), so the byte bound is real.

        A POSIX process group is not a process tree: a descendant can leave it with
        setsid() and outlive the owner. With a regular file it could keep growing the
        capture on disk after the run returned (#436 review). Here everything written
        is read by the controller under the limit, and ``finish`` closes the read ends,
        so a leftover writer gets EPIPE instead of disk.
        """

        def __init__(self, proc: subprocess.Popen[bytes], limit: int | None) -> None:
            if proc.stdout is None or proc.stderr is None:
                raise RuntimeError("pipe captures need stdout and stderr pipes")
            self.limit = limit
            self._streams = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
            self._order = (proc.stdout.fileno(), proc.stderr.fileno())
            self._files = (proc.stdout, proc.stderr)
            self._open = set(self._order)
            # poll/epoll/kqueue through selectors: select() refuses descriptors at or
            # above FD_SETSIZE, which a long-lived controller reaches (#436 review).
            self._selector = selectors.DefaultSelector()
            for fd in self._order:
                os.set_blocking(fd, False)
                self._selector.register(fd, selectors.EVENT_READ)

        def pump(self, proc: subprocess.Popen[bytes], wait_s: float) -> None:
            if not self._open:
                # Both streams reached EOF; only the exit is left to wait for.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=wait_s)
                return
            # The child's exit closes its write ends, which wakes this at once.
            for key, _events in self._selector.select(wait_s):
                self._drain(int(key.fd))

        def _drain(self, fd: int) -> None:
            while fd in self._open:
                if self.limit is not None and self.total() > self.limit:
                    return  # Past the bound: stop reading; the owner ends the tree.
                try:
                    chunk = os.read(fd, _PIPE_READ_CHUNK)
                except BlockingIOError:
                    return
                if not chunk:
                    self._open.discard(fd)
                    self._selector.unregister(fd)
                    return
                self._streams[fd].extend(chunk)

        def total(self) -> int:
            return sum(len(buffer) for buffer in self._streams.values())

        def finish(self) -> tuple[bytes, bytes]:
            for fd in self._order:
                self._drain(fd)
            self._selector.close()
            for stream in self._files:
                with contextlib.suppress(OSError):
                    stream.close()
            self._open.clear()
            return bytes(self._streams[self._order[0]]), bytes(self._streams[self._order[1]])


def _new_captures(proc: subprocess.Popen[bytes], stdout_file: IO[bytes],
                  stderr_file: IO[bytes], limit: int | None) -> _Captures:
    if sys.platform != "win32":
        captures: _Captures = _PipeCaptures(proc, limit)
    else:
        captures = _FileCaptures(stdout_file, stderr_file, limit)
    return captures


# On POSIX the child is a tiny shell gate that runs the command only after the owner
# says so, so an ``on_spawn`` callback really runs before any effect: if the callback
# fails, the gate exits 125 without running anything (#436 review). The gate is the
# shell's stdin (POSIX sh only redirects descriptors 0-9 portably); the command then
# gets /dev/null, never the controller's own stdin. "$@" carries it unchanged.
_POSIX_GATE = 'IFS= read -r _ || exit 125; exec </dev/null; exec "$@"'


@dataclass
class _Tree:
    """Mutable lifecycle state of one owned run."""

    job: windows_job.ProcessTreeJob | None
    containment: str
    timed_out: bool = False
    cancel_requested: bool = False
    cleanup_confirmed: bool | None = None
    stray_descendants: int | None = None
    strays_unconfirmed: bool = False
    # Aggregate bytes written when the output limit was crossed while running.
    output_exceeded: int | None = None


def _open_tree() -> _Tree:
    job: windows_job.ProcessTreeJob | None = None
    if windows_job.supported():
        try:
            job = windows_job.ProcessTreeJob()
        except windows_job.JobContainmentError:
            job = None
    containment = (
        "job_object" if job is not None
        else ("visible_tree" if os.name == "nt" else "process_group")
    )
    return _Tree(job, containment)


def _end_after_failure(proc: subprocess.Popen[bytes], tree: _Tree) -> None:
    """End a started tree because control is leaving abnormally; never raises."""
    with contextlib.suppress(Exception):
        tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
    with contextlib.suppress(Exception):
        _bounded_reap(proc)


def _end_unadopted(proc: subprocess.Popen[bytes], tree: _Tree) -> bool:
    """End a suspended child that the empty Job Object never owned."""
    job = tree.job
    if job is None:
        raise RuntimeError("unadopted cleanup requires an open Job Object")
    ended = windows_job.terminate_unadopted(proc, _POST_KILL_WAIT_S)
    tree.cleanup_confirmed = ended
    job.close()
    tree.job = None
    tree.containment = "visible_tree"
    return ended


def _start(spawn: Callable[[bool], subprocess.Popen[bytes]], tree: _Tree,
           on_spawn: Callable[[], None] | None,
           release: Callable[[], None] | None = None) -> subprocess.Popen[bytes]:
    proc = spawn(tree.job is not None)
    adopted = tree.job is None
    pre_adoption_cleanup_attempted = False
    try:
        if on_spawn is not None:
            # From here a process has existed, so cleanup is a real question. The
            # callback runs before the command executes: suspended on Windows, held
            # at the shell gate on POSIX. A durable "started" record precedes any effect.
            on_spawn()
        if release is not None:
            release()
        if tree.job is not None:
            try:
                tree.job.adopt_suspended(proc.pid)
            except windows_job.JobContainmentError as adoption_error:
                # The Popen remains caller-owned until adoption succeeds. OpenProcess
                # can fail before the empty job has any authority over this suspended
                # child, so end it directly before closing the job and retrying.
                ended = _end_unadopted(proc, tree)
                pre_adoption_cleanup_attempted = True
                if not ended:
                    raise windows_job.JobContainmentError(
                        0, "unadopted suspended process did not exit within cleanup bound"
                    ) from adoption_error
                proc = spawn(False)
            else:
                adopted = True
    except BaseException:
        # A raising callback (or anything else here) must not strand the child. It has
        # not run the command (POSIX gate, Windows suspension); end it all the same.
        if tree.job is not None and not adopted:
            _end_unadopted(proc, tree)
        elif not pre_adoption_cleanup_attempted:
            _end_after_failure(proc, tree)
        raise
    return proc


def _wait(proc: subprocess.Popen[bytes], tree: _Tree, deadline: float,
          probe: CancellationProbe | None, captures: _Captures) -> int:
    while True:
        try:
            if _probe_requested(probe):
                tree.cancel_requested = True
                tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
                _bounded_reap(proc)
                return 130
            exceeded = _over_limit(captures)
        except BaseException:
            # A broken cancellation source must not strand a child process.
            _end_after_failure(proc, tree)
            raise
        if exceeded is not None:
            tree.output_exceeded = exceeded
            tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
            _bounded_reap(proc)
            return 125

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            tree.timed_out = True
            tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
            _bounded_reap(proc)
            return 124
        try:
            captures.pump(proc, min(_CANCEL_POLL_S, remaining))
        except BaseException:
            _end_after_failure(proc, tree)
            raise
        code = proc.poll()
        if code is not None:
            return int(code)


def _settled_job_count(job: windows_job.ProcessTreeJob) -> int | None:
    """Active processes left in the job once its own accounting has had time to drain."""
    deadline = time.monotonic() + _STRAY_SETTLE_S
    while True:
        try:
            count = job.active_processes()
        except windows_job.JobContainmentError:
            return None
        if count == 0 or time.monotonic() >= deadline:
            return count
        time.sleep(_CANCEL_POLL_S)


if sys.platform != "win32":

    def _group_members(pgid: int) -> list[psutil.Process]:
        """Live (non-zombie) processes still in a POSIX process group."""
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return []  # The cheap, common answer: nothing at all is left in the group.
        except PermissionError:
            pass  # Something is there that we may not signal; enumerate to find out.
        members: list[psutil.Process] = []
        for candidate in psutil.process_iter():
            try:
                if os.getpgid(candidate.pid) != pgid:
                    continue
                if candidate.status() == psutil.STATUS_ZOMBIE:
                    continue
            except (psutil.Error, OSError):
                continue
            members.append(candidate)
        return members

    def _settled_group_members(pgid: int) -> list[psutil.Process]:
        deadline = time.monotonic() + _STRAY_SETTLE_S
        while True:
            members = _group_members(pgid)
            if not members or time.monotonic() >= deadline:
                return members
            time.sleep(_CANCEL_POLL_S)

    def _end_group_strays(pgid: int, tree: _Tree) -> None:
        """End what a normally exiting command left in its session's process group."""
        members = _settled_group_members(pgid)
        tree.stray_descendants = len(members)
        if not members:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        deadline = time.monotonic() + _STRAY_DRAIN_S
        while _group_members(pgid):
            if time.monotonic() >= deadline:
                tree.strays_unconfirmed = True
                break
            time.sleep(_CANCEL_POLL_S)
        # The group is empty, but a descendant could have left it with setsid() first,
        # so POSIX cleanup is never confirmed; only the strays we saw are known gone.
        tree.cleanup_confirmed = False


def _end_strays(proc: subprocess.Popen[bytes], tree: _Tree) -> None:
    """End descendants a normally exiting command abandoned in its tree."""
    if tree.timed_out or tree.cancel_requested or tree.output_exceeded is not None:
        return
    if tree.job is not None:
        # The direct child is gone. Anything left in the job is a descendant it
        # abandoned; it may still hold the captures open, so end it now rather
        # than let it outlive the result.
        tree.stray_descendants = _settled_job_count(tree.job)
        if tree.stray_descendants != 0:
            # Confirmed only by the job's own accounting reaching zero. On a
            # loaded machine that can take longer than a kill after a timeout
            # is allowed, and the result used to be ignored: the run then
            # reported job containment with the descendant still running.
            tree.cleanup_confirmed = tree.job.terminate_and_confirm(_STRAY_DRAIN_S)
            tree.strays_unconfirmed = not tree.cleanup_confirmed
        return
    if sys.platform != "win32":
        # start_new_session=True made the direct child the leader of group proc.pid.
        _end_group_strays(proc.pid, tree)
    else:
        # Windows without a job (visible_tree): the direct child is gone, so its
        # descendants can no longer be enumerated, and one could still hold the private
        # capture file and grow it after return. That is not proof of an empty tree, so
        # effect-owning callers must not count this run as finished (#436 review).
        tree.strays_unconfirmed = True


def _admitted_executable(command: list[str], lifecycle: OwnedLifecycle, *,
                         stdin: bytes | None) -> str:
    """Validate one run before anything starts; the resolved executable."""
    if not command:
        raise ValueError("empty command")
    if lifecycle.timeout_s <= 0:
        raise ValueError("command timeout must be positive")
    if lifecycle.on_spawn is not None and stdin is not None:
        # On POSIX the pre-effect gate is the child's stdin; no caller needs both.
        raise ValueError("on_spawn cannot be combined with stdin input")
    if _probe_requested(lifecycle.cancellation_probe):
        raise CommandCancellationRequested("command cancellation was requested before spawn")
    exe = shutil.which(command[0])
    if exe is None:
        raise CommandSpawnError(f"{command[0]!r} is not on PATH", missing_executable=True)
    return exe


def _settle_strays(proc: subprocess.Popen[bytes], tree: _Tree) -> None:
    try:
        _end_strays(proc, tree)
    except BaseException:
        _end_after_failure(proc, tree)
        raise


def _refuse_past_limit(tree: _Tree, total: int, limit: int | None) -> None:
    """Raise once the tree is ended if stdout and stderr together passed the limit."""
    exceeded = tree.output_exceeded
    if exceeded is None and limit is not None and total > limit:
        exceeded = total
    if exceeded is not None:
        raise CommandOutputLimitError(
            f"command wrote {exceeded} bytes, over the {limit}-byte limit for stdout and "
            "stderr together; it was ended "
            f"(process-tree cleanup confirmed={str(tree.cleanup_confirmed).lower()})",
            cleanup_confirmed=tree.cleanup_confirmed,
            containment=tree.containment,
        )


def _gated(argv: list[str]) -> list[str]:
    return [shutil.which("sh") or "/bin/sh", "-c", _POSIX_GATE, "lca-gate", *argv]


def _spawner(
    argv: list[str], cwd: Path, env: dict[str, str], *,
    stdin: int | IO[bytes] | None,
    captures: tuple[IO[bytes], IO[bytes]] | None,
) -> Callable[[bool], subprocess.Popen[bytes]]:
    """How to start the command: pipes on POSIX (``captures`` None), files on Windows."""

    def spawn(suspended: bool) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - resolved executable, fixed argv
            argv,
            cwd=str(cwd),
            env=env,
            stdin=stdin,
            stdout=subprocess.PIPE if captures is None else captures[0],
            stderr=subprocess.PIPE if captures is None else captures[1],
            start_new_session=captures is None,
            creationflags=(windows_job.CREATE_SUSPENDED if suspended else 0),
        )

    return spawn


@dataclass(frozen=True)
class OwnedLifecycle:
    """How long an owned run may last, who may stop it and how much output is read."""

    timeout_s: float
    cancellation_probe: CancellationProbe | None = None
    on_spawn: Callable[[], None] | None = None
    output_limit: int | None = None


def run_owned(
    command: list[str],
    cwd: Path,
    lifecycle: OwnedLifecycle,
    *,
    env: dict[str, str],
    stdin: bytes | None = None,
) -> OwnedRun:
    """Run one command as an owned process tree and return its output in memory.

    This is the one owner of spawn, containment, timeout, cancellation, reap and stray
    accounting; ``run_command`` adds the public run-directory evidence on top of it.
    ``lifecycle`` bounds it; ``env`` is the complete environment. ``stdin`` is given to
    the child as a file, so feeding it can never block past the deadline; ``None``
    inherits the caller's stdin. Once stdout and stderr together pass ``output_limit``
    bytes the tree is ended and ``CommandOutputLimitError`` is raised; output is never
    truncated into a result.
    """
    exe = _admitted_executable(command, lifecycle, stdin=stdin)
    started = time.monotonic()
    tree = _open_tree()
    gate: tuple[int, int] | None = None
    gate_open: set[int] = set()
    try:
        # Captures never live inside a public run directory: private files on Windows
        # (the job ends every writer), controller-drained pipes on POSIX.
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
            tempfile.TemporaryFile(mode="w+b") as stdin_file,
        ):
            if stdin is not None:
                stdin_file.write(stdin)
                stdin_file.seek(0)
            posix = sys.platform != "win32"
            if posix and lifecycle.on_spawn is not None:
                gate = os.pipe()
                gate_open.update(gate)
            argv = [exe, *command[1:]]
            spawn = _spawner(
                _gated(argv) if gate is not None else argv, cwd, env,
                stdin=gate[0] if gate is not None else (stdin_file if stdin is not None else None),
                captures=(None if posix else (stdout_file, stderr_file)),
            )

            def release() -> None:
                if gate is not None:
                    os.write(gate[1], b"go\n")

            proc = _start(spawn, tree, lifecycle.on_spawn, release)
            if gate is not None:
                # The child holds its own copy; ours would only keep the gate alive.
                os.close(gate[0])
                gate_open.discard(gate[0])
            captures = _new_captures(proc, stdout_file, stderr_file, lifecycle.output_limit)
            code = _wait(
                proc, tree, started + lifecycle.timeout_s, lifecycle.cancellation_probe,
                captures,
            )
            _settle_strays(proc, tree)
            # Output is fixed here: nothing a leftover process writes later is accepted.
            stdout, stderr = captures.finish()
            _refuse_past_limit(tree, len(stdout) + len(stderr), lifecycle.output_limit)
    except OSError as exc:
        raise CommandSpawnError(
            f"could not start {exe!r}: {exc.strerror or exc}", missing_executable=False
        ) from exc
    finally:
        # Each gate descriptor is closed exactly once; a number closed twice could
        # belong to an unrelated file by then.
        for fd in gate_open:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tree.job is not None:
            tree.job.close()

    return OwnedRun(
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        elapsed_s=time.monotonic() - started,
        timed_out=tree.timed_out,
        cancel_requested=tree.cancel_requested,
        cleanup_confirmed=tree.cleanup_confirmed,
        containment=tree.containment,
        stray_descendants_at_exit=tree.stray_descendants,
        strays_unconfirmed=tree.strays_unconfirmed,
    )


# What a configured build or test command may write to stdout and stderr together. The
# controller holds it in memory, so it is a hard bound, and crossing it ends the tree.
COMMAND_OUTPUT_LIMIT = 256 * 1024 * 1024


def run_command(
    command: list[str],
    cwd: Path,
    run_root: Path,
    timeout_s: int,
    env_overrides: dict[str, str] | None = None,
    cancellation_probe: CancellationProbe | None = None,
    on_spawn: Callable[[], None] | None = None,
) -> RunOutcome:
    """Run one command; ``on_spawn`` is called once, as soon as a process exists."""
    if not command:
        raise ValueError("empty command")
    if timeout_s <= 0:
        raise ValueError("command timeout must be positive")
    if _probe_requested(cancellation_probe):
        raise CommandCancellationRequested("command cancellation was requested before spawn")

    from .tool_primitives import BlockedError, Reason, ToolError

    if shutil.which(command[0]) is None:
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

    try:
        run = run_owned(
            command, cwd,
            OwnedLifecycle(timeout_s, cancellation_probe=cancellation_probe, on_spawn=on_spawn,
                           output_limit=COMMAND_OUTPUT_LIMIT),
            env=env,
        )
    except CommandSpawnError as exc:
        reason = Reason.MISSING_EXECUTABLE if exc.missing_executable else Reason.SPAWN_FAILURE
        raise BlockedError(str(exc), reason) from exc
    except CommandOutputLimitError as exc:
        (run_dir / "command.txt").write_text(
            " ".join(command) + f"\noutput limit: {exc}\n", encoding="utf-8",
        )
        raise ToolError(
            f"the command was ended because it wrote more output than Local Code Agent "
            f"reads ({COMMAND_OUTPUT_LIMIT // (1024 * 1024)} MiB): {exc}. Its output is not "
            "a result.",
            Reason.OUTPUT_LIMIT,
        ) from exc

    stdout = run.stdout.decode("utf-8", errors="replace")
    stderr = run.stderr.decode("utf-8", errors="replace")
    stderr += _lifecycle_note(run, timeout_s)
    _write_run_evidence(run_dir, command, run, stdout, stderr)
    if run.strays_unconfirmed:
        # The single public process boundary fails closed: a run whose tree was not
        # shown ended is never handed to a build or test verdict as `ok` (#436 review).
        # The evidence above is kept; the exit code is not a result.
        raise ToolError(
            f"the command exited (code {run.exit_code}) but its process tree could not be "
            f"shown ended (containment={run.containment}); its output is not a result. "
            f"Evidence: {relative_run_dir(run_root, run_dir)}",
            Reason.CLEANUP_UNKNOWN,
        )

    return RunOutcome(
        command=command,
        exit_code=run.exit_code,
        elapsed_s=run.elapsed_s,
        timed_out=run.timed_out,
        run_id=run_id,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        combined_path=combined_path,
        process_cleanup_confirmed=run.cleanup_confirmed,
        cancel_requested=run.cancel_requested,
        containment=run.containment,
        stray_descendants_at_exit=run.stray_descendants_at_exit,
    )


def relative_run_dir(run_root: Path, run_dir: Path) -> str:
    try:
        return run_dir.relative_to(run_root).as_posix()
    except ValueError:
        return run_dir.as_posix()


def _lifecycle_note(run: OwnedRun, timeout_s: float) -> str:
    note = ""
    if run.strays_unconfirmed:
        note += (
            "\n[local-agent] the command exited but left descendant "
            f"process(es) in its job; termination was not confirmed "
            f"within {_STRAY_DRAIN_S:.0f}s\n"
        )
    if run.timed_out:
        note += (
            f"\n[local-agent] command exceeded {timeout_s}s; "
            f"process-tree cleanup confirmed={str(run.cleanup_confirmed).lower()}\n"
        )
    if run.cancel_requested:
        note += (
            "\n[local-agent] command cancellation requested; "
            f"process-tree cleanup confirmed={str(run.cleanup_confirmed).lower()}\n"
        )
    return note


def _write_run_evidence(run_dir: Path, command: list[str], run: OwnedRun,
                        stdout: str, stderr: str) -> None:
    (run_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    (run_dir / "combined.log").write_text(stdout + stderr, encoding="utf-8")
    (run_dir / "command.txt").write_text(
        " ".join(command)
        + f"\nexit={run.exit_code} elapsed={run.elapsed_s:.2f}s "
        + f"timed_out={str(run.timed_out).lower()} "
        + f"cancel_requested={str(run.cancel_requested).lower()} "
        + f"cleanup_confirmed={run.cleanup_confirmed} containment={run.containment} "
        + f"stray_descendants_at_exit={run.stray_descendants_at_exit}\n",
        encoding="utf-8",
    )
