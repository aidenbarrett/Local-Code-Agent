"""Subprocess execution.

Every command runs with a fixed argv from the repository config, a working
directory pinned to the repository root, a timeout, and its output captured to
disk rather than into the context window.

Timeout is an execution-state claim, not just a return code. A POSIX session gives
us a useful process group to kill, but it is not containment: a descendant can
call ``setsid()`` and escape that group. Windows process enumeration has the same
proof problem until the runner owns a Job Object. Timeout cleanup is therefore
reported as unconfirmed on both platforms unless stronger OS containment exists.

Child output is written to private temporary files and snapshotted into the
public run artifacts only after the direct child exits or timeout handling
completes. An escaped descendant can therefore neither hold ``communicate()``
open forever nor mutate the evidence files after ``run_command`` returns.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import psutil


_POST_KILL_WAIT_S = 2.0
_POST_KILL_FORCE_WAIT_S = 1.0


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
    # None means no timeout cleanup was required. False means best-effort
    # cleanup ran but the runner cannot prove whole-tree containment. True is
    # reserved for a future implementation that owns the process tree with a
    # primitive such as a Linux cgroup or Windows Job Object.
    process_cleanup_confirmed: bool | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def new_run_dir(run_root: Path) -> tuple[str, Path]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = run_root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return run_id, path


def _best_effort_windows_tree_kill(proc: subprocess.Popen) -> None:
    """Reconcile the visible tree, without claiming Job-Object containment."""
    try:
        parent = psutil.Process(proc.pid)
        descendants = parent.children(recursive=True)
    except psutil.Error:
        descendants = []
        parent = None

    targets = descendants + ([parent] if parent is not None else [])
    for target in reversed(targets):
        try:
            target.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs([p for p in targets if p is not None], timeout=1.0)
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=1.0)
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _kill_timed_out_process(proc: subprocess.Popen) -> bool:
    """Best-effort timeout cleanup; return whether whole-tree cleanup is proven."""
    if os.name == "nt":
        _best_effort_windows_tree_kill(proc)
        # Enumeration is not containment. A Windows Job Object is required before
        # this runner may return True here.
        return False

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # start_new_session=True makes the direct child the process-group leader, so
    # killpg reliably kills processes that stayed in that group. It cannot prove
    # that a descendant did not call setsid()/setpgid() first and escape. A cgroup
    # (or equivalent containment primitive) is required before POSIX may return
    # True here.
    return False


def _bounded_reap(proc: subprocess.Popen) -> None:
    """Reap the direct child without ever turning timeout cleanup into a hang."""
    try:
        proc.wait(timeout=_POST_KILL_WAIT_S)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_POST_KILL_FORCE_WAIT_S)
    except subprocess.TimeoutExpired:
        # The timeout result remains fail-closed. Returning is preferable to
        # hanging the controller while pretending the process tree was owned.
        pass


def _snapshot_capture(capture: BinaryIO) -> str:
    """Read exactly the bytes present at one instant, even if a child keeps writing."""
    size = os.fstat(capture.fileno()).st_size
    capture.seek(0)
    return capture.read(size).decode("utf-8", errors="replace")


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
    try:
        # These captures never live inside the public run directory. If an
        # escaped descendant retains its inherited descriptor, it can only keep
        # writing to the private temporary object; the public evidence below is
        # a bounded snapshot of a fixed byte length.
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_capture,
            tempfile.TemporaryFile(mode="w+b") as stderr_capture,
        ):
            proc = subprocess.Popen(
                [exe, *command[1:]],
                cwd=str(cwd),
                env=env,
                stdout=stdout_capture,
                stderr=stderr_capture,
                start_new_session=(os.name != "nt"),
            )
            try:
                code = int(proc.wait(timeout=timeout_s))
            except subprocess.TimeoutExpired:
                timed_out = True
                cleanup_confirmed = _kill_timed_out_process(proc)
                _bounded_reap(proc)
                code = 124

            stdout = _snapshot_capture(stdout_capture)
            stderr = _snapshot_capture(stderr_capture)
    except OSError as exc:
        raise BlockedError(
            f"could not start {exe!r}: {exc.strerror or exc}", Reason.SPAWN_FAILURE
        ) from exc

    if timed_out:
        stderr += (
            f"\n[local-agent] command exceeded {timeout_s}s; "
            f"process-tree cleanup confirmed={str(cleanup_confirmed).lower()}\n"
        )

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
