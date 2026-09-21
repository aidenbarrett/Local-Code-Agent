"""Truthful process-tree stop adapter for POSIX process-group containment.

Cancellation may request that an owned process tree stop, but terminalisation needs
proof. This adapter checks the observed leader birth identity before signalling the
owned process group, escalates TERM to KILL within explicit bounds, and reports only
what it can prove. Unsupported containment never masquerades as cleanup success.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
from time import monotonic, sleep
from typing import Callable

import psutil

from .cancellation import OwnedProcessHandle, ProcessContainment


BirthTokenFn = Callable[[int], str | None]
GroupAliveFn = Callable[[int], bool]
SignalGroupFn = Callable[[int, int], None]
ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]


def process_birth_token(pid: int) -> str | None:
    """Return a stable-enough process identity token for one observed PID."""
    try:
        process = psutil.Process(pid)
        return f"{pid}:{process.create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def posix_group_alive(pgid: int) -> bool:
    """Whether the kernel currently exposes any process in the group."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but this process cannot signal it. That is still alive.
        return True
    return True


@dataclass(frozen=True)
class ProcessStopResult:
    attempted: bool
    known_stopped: bool
    reason: str
    term_sent: bool
    kill_sent: bool
    elapsed_ms: int


class PosixProcessGroupStopper:
    """Stop only process groups whose leader identity still matches our handle."""

    def __init__(
        self,
        *,
        birth_token: BirthTokenFn = process_birth_token,
        group_alive: GroupAliveFn = posix_group_alive,
        signal_group: SignalGroupFn = os.killpg,
        clock: ClockFn = monotonic,
        sleeper: SleepFn = sleep,
        poll_interval_s: float = 0.05,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("process-stop poll interval must be positive")
        self.birth_token = birth_token
        self.group_alive = group_alive
        self.signal_group = signal_group
        self.clock = clock
        self.sleeper = sleeper
        self.poll_interval_s = float(poll_interval_s)

    def _result(
        self,
        started: float,
        *,
        attempted: bool,
        known_stopped: bool,
        reason: str,
        term_sent: bool = False,
        kill_sent: bool = False,
    ) -> ProcessStopResult:
        return ProcessStopResult(
            attempted=attempted,
            known_stopped=known_stopped,
            reason=reason,
            term_sent=term_sent,
            kill_sent=kill_sent,
            elapsed_ms=max(0, int((self.clock() - started) * 1000)),
        )

    def _wait_stopped(self, pgid: int, deadline: float) -> bool:
        while True:
            if not self.group_alive(pgid):
                return True
            now = self.clock()
            if now >= deadline:
                return False
            self.sleeper(min(self.poll_interval_s, max(0.0, deadline - now)))

    def stop(
        self,
        handle: OwnedProcessHandle,
        *,
        term_grace_s: float = 2.0,
        kill_grace_s: float = 2.0,
    ) -> ProcessStopResult:
        if term_grace_s < 0 or kill_grace_s < 0:
            raise ValueError("process-stop grace periods must be non-negative")
        started = self.clock()
        if handle.containment is not ProcessContainment.POSIX_PROCESS_GROUP:
            return self._result(
                started,
                attempted=False,
                known_stopped=False,
                reason="unsupported_containment",
            )
        pgid = int(handle.containment_id)

        alive = self.group_alive(pgid)
        if not alive:
            return self._result(
                started,
                attempted=False,
                known_stopped=True,
                reason="already_stopped",
            )

        current_birth = self.birth_token(handle.pid)
        if current_birth is None:
            # The leader disappeared while the group still exists. Signalling by PGID
            # now risks touching a reused group whose ownership we cannot prove.
            return self._result(
                started,
                attempted=False,
                known_stopped=False,
                reason="owner_identity_unavailable",
            )
        if current_birth != handle.birth_token:
            return self._result(
                started,
                attempted=False,
                known_stopped=False,
                reason="owner_identity_changed",
            )

        term_sent = False
        try:
            self.signal_group(pgid, signal.SIGTERM)
            term_sent = True
        except ProcessLookupError:
            return self._result(
                started,
                attempted=True,
                known_stopped=not self.group_alive(pgid),
                reason="stopped_during_term" if not self.group_alive(pgid) else "term_target_changed",
            )
        except (PermissionError, OSError):
            return self._result(
                started,
                attempted=True,
                known_stopped=False,
                reason="term_signal_failed",
            )

        if self._wait_stopped(pgid, self.clock() + float(term_grace_s)):
            return self._result(
                started,
                attempted=True,
                known_stopped=True,
                reason="stopped_after_term",
                term_sent=True,
            )

        kill_sent = False
        try:
            self.signal_group(pgid, signal.SIGKILL)
            kill_sent = True
        except ProcessLookupError:
            stopped = not self.group_alive(pgid)
            return self._result(
                started,
                attempted=True,
                known_stopped=stopped,
                reason="stopped_during_kill" if stopped else "kill_target_changed",
                term_sent=term_sent,
            )
        except (PermissionError, OSError):
            return self._result(
                started,
                attempted=True,
                known_stopped=False,
                reason="kill_signal_failed",
                term_sent=term_sent,
            )

        stopped = self._wait_stopped(pgid, self.clock() + float(kill_grace_s))
        return self._result(
            started,
            attempted=True,
            known_stopped=stopped,
            reason="stopped_after_kill" if stopped else "process_group_still_alive",
            term_sent=term_sent,
            kill_sent=kill_sent,
        )
