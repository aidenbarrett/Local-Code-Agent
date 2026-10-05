"""Subprocess execution.

Every command runs with a fixed argv from the repository config, a working
directory pinned to the repository root, a timeout, and its output captured to
disk rather than into the context window.

Timeout and cancellation are execution-state claims, not just return codes. A POSIX
session gives us a useful process group to kill, but it is not containment: a descendant
can call ``setsid()`` and escape that group, so POSIX cleanup is reported unconfirmed.

On Windows the command is created suspended and adopted by a kill-on-close Job Object
before its first instruction runs (``windows_job``). Descendants cannot break away, and
cleanup is confirmed only when the kernel's job accounting reports zero active
processes. If the OS refuses the job, the runner falls back to visible-tree enumeration
and reports cleanup unconfirmed, exactly as before. Descendants still alive when the
direct child exits normally are terminated with the job and counted, so a finished
command never leaves an owned process behind.

Child output is written to private temporary files and snapshotted into the
public run artifacts only after the direct child exits or timeout/cancellation handling
completes. An escaped descendant can therefore neither hold ``communicate()`` open forever
nor mutate the evidence files after ``run_command`` returns.
"""

from __future__ import annotations

import contextlib
import mmap
import os
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
    # Descendants still inside the job when the direct child exited normally, which
    # the runner then terminated. None when the containment cannot count them.
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
    """Aggregate command output crossed the execution bound."""

    def __init__(
        self, observed_bytes: int, limit: int, *, cleanup_confirmed: bool | None,
    ) -> None:
        self.observed_bytes = observed_bytes
        self.limit = limit
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__(
            f"command wrote {observed_bytes} aggregate bytes, over the {limit}-byte "
            f"limit; process-tree cleanup confirmed={cleanup_confirmed}"
        )


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


def _snapshot_bytes(capture: IO[bytes]) -> bytes:
    """Read a fixed-length snapshot without moving an inherited file offset."""
    size = os.fstat(capture.fileno()).st_size
    if size == 0:
        return b""
    with mmap.mmap(capture.fileno(), length=size, access=mmap.ACCESS_READ) as view:
        return view[:]


def _capture_size(captures: tuple[IO[bytes], IO[bytes]]) -> int:
    return sum(os.fstat(capture.fileno()).st_size for capture in captures)


def _enforce_output_limit(
    proc: subprocess.Popen[bytes],
    tree: _Tree,
    captures: tuple[IO[bytes], IO[bytes]],
    limit: int | None,
) -> None:
    if limit is None:
        return
    observed = _capture_size(captures)
    if observed <= limit:
        return
    tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
    _bounded_reap(proc)
    raise CommandOutputLimitError(
        observed, limit, cleanup_confirmed=tree.cleanup_confirmed
    )


def _os_int(name: str) -> int | None:
    value = getattr(os, name, None)
    return value if isinstance(value, int) else None


def _posix_exit_ready(proc: subprocess.Popen[bytes]) -> bool | None:
    """Observe exit without reaping the group leader when supported."""
    waitid = getattr(os, "waitid", None)
    p_pid = _os_int("P_PID")
    wexited = _os_int("WEXITED")
    wnohang = _os_int("WNOHANG")
    wnowait = _os_int("WNOWAIT")
    if (
        not callable(waitid)
        or p_pid is None
        or wexited is None
        or wnohang is None
        or wnowait is None
    ):
        return None
    try:
        flags = int(wexited) | int(wnohang) | int(wnowait)
        return waitid(int(p_pid), proc.pid, flags) is not None
    except ChildProcessError:
        return None


def _posix_group_members(pgid: int, leader_pid: int) -> tuple[int, ...]:
    getpgid = getattr(os, "getpgid", None)
    if not callable(getpgid):
        return ()
    members: list[int] = []
    for process in psutil.process_iter(["pid", "status"]):
        if process.pid == leader_pid:
            continue
        try:
            if getpgid(process.pid) == pgid and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process.pid)
        except (OSError, psutil.Error):
            continue
    return tuple(members)


def _wait_members_gone(pids: tuple[int, ...], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        alive: list[int] = []
        for pid in pids:
            try:
                if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                    alive.append(pid)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                alive.append(pid)
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_CANCEL_POLL_S)


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


@dataclass(frozen=True)
class _WaitContext:
    deadline: float
    probe: CancellationProbe | None
    captures: tuple[IO[bytes], IO[bytes]]
    output_limit: int | None


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


def _start(spawn: Callable[[bool], subprocess.Popen[bytes]], tree: _Tree,
           on_spawn: Callable[[], None] | None) -> subprocess.Popen[bytes]:
    proc = spawn(tree.job is not None)
    try:
        if on_spawn is not None:
            on_spawn()
    except BaseException:
        if tree.job is not None:
            with contextlib.suppress(OSError):
                proc.kill()
        else:
            _kill_process_tree_best_effort(proc)
        _bounded_reap(proc)
        raise
    if tree.job is not None:
        try:
            tree.job.adopt_suspended(proc.pid)
        except windows_job.JobContainmentError:
            _bounded_reap(proc)
            tree.job.close()
            tree.job = None
            tree.containment = "visible_tree"
            proc = spawn(False)
    return proc


def _wait(
    proc: subprocess.Popen[bytes], tree: _Tree, context: _WaitContext,
) -> int | None:
    """Wait while preserving safe POSIX group identity until stray cleanup."""
    while True:
        try:
            _enforce_output_limit(
                proc, tree, context.captures, context.output_limit
            )
            if _probe_requested(context.probe):
                tree.cancel_requested = True
                tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
                _bounded_reap(proc)
                return 130
        except BaseException:
            if proc.poll() is None:
                _kill_process_tree_best_effort(proc, tree.job)
                _bounded_reap(proc)
            raise

        remaining = context.deadline - time.monotonic()
        if remaining <= 0:
            tree.timed_out = True
            tree.cleanup_confirmed = _kill_process_tree_best_effort(proc, tree.job)
            _bounded_reap(proc)
            return 124

        if os.name != "nt":
            ready = _posix_exit_ready(proc)
            if ready is True:
                _enforce_output_limit(
                    proc, tree, context.captures, context.output_limit
                )
                return None
            if ready is None:
                try:
                    code = int(proc.wait(timeout=min(_CANCEL_POLL_S, remaining)))
                except subprocess.TimeoutExpired:
                    continue
                _enforce_output_limit(
                    proc, tree, context.captures, context.output_limit
                )
                tree.strays_unconfirmed = True
                return code
            time.sleep(min(_CANCEL_POLL_S, remaining))
            continue

        try:
            code = int(proc.wait(timeout=min(_CANCEL_POLL_S, remaining)))
        except subprocess.TimeoutExpired:
            continue
        _enforce_output_limit(
            proc, tree, context.captures, context.output_limit
        )
        return code


def _end_strays(proc: subprocess.Popen[bytes], tree: _Tree) -> None:
    """End descendants abandoned by a normally exiting direct child."""
    if tree.timed_out or tree.cancel_requested:
        return
    if tree.job is not None:
        try:
            tree.stray_descendants = tree.job.active_processes()
        except windows_job.JobContainmentError:
            tree.stray_descendants = None
        if tree.stray_descendants != 0:
            tree.cleanup_confirmed = tree.job.terminate_and_confirm(_STRAY_DRAIN_S)
            tree.strays_unconfirmed = not tree.cleanup_confirmed
        return
    if os.name == "nt":
        tree.stray_descendants = None
        tree.strays_unconfirmed = True
        return
    if tree.strays_unconfirmed:
        return

    members = _posix_group_members(proc.pid, proc.pid)
    tree.stray_descendants = len(members)
    if not members:
        return
    killpg = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if callable(killpg) and sigkill is not None:
        with contextlib.suppress(ProcessLookupError):
            killpg(proc.pid, sigkill)
    _wait_members_gone(members, _POST_KILL_WAIT_S)
    tree.cleanup_confirmed = False
    tree.strays_unconfirmed = True


@dataclass(frozen=True)
class OwnedLifecycle:
    """How long an owned run may last, who may stop it and how much output is read."""

    timeout_s: float
    cancellation_probe: CancellationProbe | None = None
    on_spawn: Callable[[], None] | None = None
    # Aggregate stdout + stderr execution bound, checked while running.
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
    inherits the caller's stdin. Output beyond ``output_limit`` bytes raises
    ``CommandOutputLimitError`` rather than being truncated.
    """
    if not command:
        raise ValueError("empty command")
    if lifecycle.timeout_s <= 0:
        raise ValueError("command timeout must be positive")
    if _probe_requested(lifecycle.cancellation_probe):
        raise CommandCancellationRequested("command cancellation was requested before spawn")
    exe = shutil.which(command[0])
    if exe is None:
        raise CommandSpawnError(f"{command[0]!r} is not on PATH", missing_executable=True)

    started = time.monotonic()
    tree = _open_tree()
    try:
        # These captures never live inside a public run directory. If an escaped
        # descendant retains its inherited descriptor, it can only keep writing to the
        # private temporary object; the result is a bounded snapshot of fixed length.
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_capture,
            tempfile.TemporaryFile(mode="w+b") as stderr_capture,
            tempfile.TemporaryFile(mode="w+b") as stdin_file,
        ):
            if stdin is not None:
                stdin_file.write(stdin)
                stdin_file.seek(0)

            def spawn(suspended: bool) -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    [exe, *command[1:]],
                    cwd=str(cwd),
                    env=env,
                    stdin=(stdin_file if stdin is not None else None),
                    stdout=stdout_capture,
                    stderr=stderr_capture,
                    start_new_session=(os.name != "nt"),
                    creationflags=(windows_job.CREATE_SUSPENDED if suspended else 0),
                )

            proc = _start(spawn, tree, lifecycle.on_spawn)
            captures = (stdout_capture, stderr_capture)
            wait = _WaitContext(
                started + lifecycle.timeout_s, lifecycle.cancellation_probe,
                captures, lifecycle.output_limit,
            )
            code = _wait(proc, tree, wait)
            _end_strays(proc, tree)
            if code is None:
                code = int(proc.wait(timeout=_POST_KILL_FORCE_WAIT_S))
            stdout = _snapshot_bytes(stdout_capture)
            stderr = _snapshot_bytes(stderr_capture)
    except OSError as exc:
        raise CommandSpawnError(
            f"could not start {exe!r}: {exc.strerror or exc}", missing_executable=False
        ) from exc
    finally:
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

    from .tool_primitives import BlockedError, Reason

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
            OwnedLifecycle(timeout_s, cancellation_probe=cancellation_probe, on_spawn=on_spawn),
            env=env,
        )
    except CommandSpawnError as exc:
        reason = Reason.MISSING_EXECUTABLE if exc.missing_executable else Reason.SPAWN_FAILURE
        raise BlockedError(str(exc), reason) from exc

    stdout = run.stdout.decode("utf-8", errors="replace")
    stderr = run.stderr.decode("utf-8", errors="replace")
    stderr += _lifecycle_note(run, timeout_s)
    _write_run_evidence(run_dir, command, run, stdout, stderr)

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
