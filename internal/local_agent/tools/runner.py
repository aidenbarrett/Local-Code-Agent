"""Subprocess execution.

Every command runs with a fixed argv from the repository config, a working
directory pinned to the repository root, a timeout, and its output captured to
disk rather than into the context window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


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

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def new_run_dir(run_root: Path) -> tuple[str, Path]:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    path = run_root / run_id
    path.mkdir(parents=True, exist_ok=True)
    return run_id, path


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
    try:
        proc = subprocess.run(
            [exe, *command[1:]],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except OSError as exc:
        # The executable exists (which() found it) but the OS would not start it:
        # permissions, a broken shim, an architecture mismatch. Not the model's
        # doing and not the environment lacking a tool; our spawn failed.
        raise BlockedError(
            f"could not start {exe!r}: {exc.strerror or exc}", Reason.SPAWN_FAILURE
        ) from exc
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        stderr += f"\n[local-agent] command exceeded {timeout_s}s and was killed\n"
        code = 124

    elapsed = time.monotonic() - started

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined_path.write_text(stdout + stderr, encoding="utf-8")
    (run_dir / "command.txt").write_text(
        " ".join(command) + f"\nexit={code} elapsed={elapsed:.2f}s\n",
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
    )
