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


def _snapshot_capture(capture: IO[bytes]) -> str:
    """Read a fixed-length snapshot without moving an inherited file offset."""
    size = os.fstat(capture.fileno()).st_size
    if size == 0:
        return ""
    with mmap.mmap(capture.fileno(), length=size, access=mmap.ACCESS_READ) as view:
        return view[:].decode("utf-8", errors="replace")


def _probe_requested(probe: CancellationProbe | None) -> bool:
    if probe is None:
        return False
    requested = probe.requested
    if not isinstance(requested, bool):
        raise TypeError("cancellation probe requested state must be boolean")
    return requested


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
    deadline = started + timeout_s
    timed_out = False
    cancel_requested = False
    cleanup_confirmed: bool | None = None
    stray_descendants: int | None = None
    stray_note = ""
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
    try:
        # These captures never live inside the public run directory. If an
        # escaped descendant retains its inherited descriptor, it can only keep
        # writing to the private temporary object; the public evidence below is
        # a bounded snapshot of a fixed byte length.
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_capture,
            tempfile.TemporaryFile(mode="w+b") as stderr_capture,
        ):
            def spawn(suspended: bool) -> subprocess.Popen[bytes]:
                return subprocess.Popen(
                    [exe, *command[1:]],
                    cwd=str(cwd),
                    env=env,
                    stdout=stdout_capture,
                    stderr=stderr_capture,
                    start_new_session=(os.name != "nt"),
                    creationflags=(windows_job.CREATE_SUSPENDED if suspended else 0),
                )

            proc = spawn(job is not None)
            if on_spawn is not None:
                # From here a process has existed, so cleanup is a real question.
                on_spawn()
            if job is not None:
                try:
                    job.adopt_suspended(proc.pid)
                except windows_job.JobContainmentError:
                    # adopt_suspended already terminated the suspended child, which never
                    # executed an instruction, so running the command again is not a
                    # replayed effect. Without a job, cleanup can no longer be proven.
                    _bounded_reap(proc)
                    job.close()
                    job = None
                    containment = "visible_tree"
                    proc = spawn(False)
            code: int | None = None
            while code is None:
                try:
                    if _probe_requested(cancellation_probe):
                        cancel_requested = True
                        cleanup_confirmed = _kill_process_tree_best_effort(proc, job)
                        _bounded_reap(proc)
                        code = 130
                        break
                except BaseException:
                    # A broken cancellation source must not strand a child process.
                    _kill_process_tree_best_effort(proc, job)
                    _bounded_reap(proc)
                    raise

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    cleanup_confirmed = _kill_process_tree_best_effort(proc, job)
                    _bounded_reap(proc)
                    code = 124
                    break
                try:
                    code = int(proc.wait(timeout=min(_CANCEL_POLL_S, remaining)))
                except subprocess.TimeoutExpired:
                    continue

            if job is not None and not timed_out and not cancel_requested:
                # The direct child is gone. Anything left in the job is a descendant it
                # abandoned; it may still hold the captures open, so end it now rather
                # than let it outlive the result.
                try:
                    stray_descendants = job.active_processes()
                except windows_job.JobContainmentError:
                    stray_descendants = None
                if stray_descendants != 0:
                    # Confirmed only by the job's own accounting reaching zero. On a
                    # loaded machine that can take longer than a kill after a timeout
                    # is allowed, and the result used to be ignored: the run then
                    # reported job containment with the descendant still running.
                    cleanup_confirmed = job.terminate_and_confirm(_STRAY_DRAIN_S)
                    if not cleanup_confirmed:
                        stray_note = (
                            "\n[local-agent] the command exited but left descendant "
                            f"process(es) in its job; termination was not confirmed "
                            f"within {_STRAY_DRAIN_S:.0f}s\n"
                        )

            stdout = _snapshot_capture(stdout_capture)
            stderr = _snapshot_capture(stderr_capture)
    except OSError as exc:
        raise BlockedError(
            f"could not start {exe!r}: {exc.strerror or exc}", Reason.SPAWN_FAILURE
        ) from exc
    finally:
        if job is not None:
            job.close()

    stderr += stray_note
    if timed_out:
        stderr += (
            f"\n[local-agent] command exceeded {timeout_s}s; "
            f"process-tree cleanup confirmed={str(cleanup_confirmed).lower()}\n"
        )
    if cancel_requested:
        stderr += (
            "\n[local-agent] command cancellation requested; "
            f"process-tree cleanup confirmed={str(cleanup_confirmed).lower()}\n"
        )

    elapsed = time.monotonic() - started

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined_path.write_text(stdout + stderr, encoding="utf-8")
    (run_dir / "command.txt").write_text(
        " ".join(command)
        + f"\nexit={code} elapsed={elapsed:.2f}s timed_out={str(timed_out).lower()} "
        + f"cancel_requested={str(cancel_requested).lower()} "
        + f"cleanup_confirmed={cleanup_confirmed} containment={containment} "
        + f"stray_descendants_at_exit={stray_descendants}\n",
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
        cancel_requested=cancel_requested,
        containment=containment,
        stray_descendants_at_exit=stray_descendants,
    )
