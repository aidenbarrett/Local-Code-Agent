"""Subprocess execution.

Every command runs with a fixed argv from the repository config, a working
directory pinned to the repository root, a timeout, and its output captured to
disk rather than into the context window.

Timeout is an execution-state claim, not just a return code. On POSIX we launch a
new session so the controller owns a killable process group. On Windows we still
reconcile the visible psutil tree, but without a Job Object we cannot prove that a
racing descendant did not escape enumeration; timeout cleanup is therefore
reported as unconfirmed there rather than pretending success.
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
    # None means no timeout cleanup was required. True means the owned POSIX
    # process group was killed and reaped. False means best-effort cleanup ran but
    # containment was not strong enough to certify the whole tree (Windows until
    # the runner uses a Job Object).
    process_cleanup_confirmed: bool | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def new_run_dir(run_root: Path) -> tuple[str, Path]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = run_root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return run_id, path


def _best_effort_windows_tree_kill(proc: subprocess.Popen[str]) -> None:
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


def _kill_timed_out_process(proc: subprocess.Popen[str]) -> bool:
    if os.name == "nt":
        _best_effort_windows_tree_kill(proc)
        # Enumeration is not containment. A Windows Job Object is required before
        # this runner may return True here.
        return False

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # The process is the process-group leader because start_new_session=True.
    # communicate() below reaps it; all members receive SIGKILL atomically from
    # the kernel's process-group operation.
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
    try:
        proc = subprocess.Popen(
            [exe, *command[1:]],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            code = int(proc.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_confirmed = _kill_timed_out_process(proc)
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
