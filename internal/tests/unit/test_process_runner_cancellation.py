from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from local_agent.tools.process_runner import CommandCancellationRequested, run_command


class Probe:
    def __init__(self, requested: bool = False) -> None:
        self._event = threading.Event()
        if requested:
            self._event.set()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        self._event.set()


def _wait_for_path(path: Path, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("child process did not reach the start marker")
        time.sleep(0.01)


def test_pre_requested_cancel_refuses_before_spawn(tmp_path):
    marker = tmp_path / "started.txt"
    probe = Probe(requested=True)
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('started')",
    ]

    with pytest.raises(CommandCancellationRequested, match="before spawn"):
        run_command(command, tmp_path, tmp_path / "runs", 5, cancellation_probe=probe)

    assert marker.exists() is False
    assert (tmp_path / "runs").exists() is False


def test_running_command_observes_cancel_without_claiming_whole_tree_cleanup(tmp_path):
    marker = tmp_path / "started.txt"
    probe = Probe()
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import time; "
            f"Path({str(marker)!r}).write_text('started'); "
            "time.sleep(30)"
        ),
    ]
    observed = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            observed.append(
                run_command(
                    command,
                    tmp_path,
                    tmp_path / "runs",
                    20,
                    cancellation_probe=probe,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    _wait_for_path(marker)
    probe.request()
    thread.join(5)

    assert thread.is_alive() is False
    assert errors == []
    assert len(observed) == 1
    outcome = observed[0]
    assert outcome.cancel_requested is True
    assert outcome.timed_out is False
    assert outcome.exit_code == 130
    assert outcome.ok is False
    # Neither process-group signalling on POSIX nor descendant enumeration on Windows is
    # whole-tree containment. A cancellation request must not upgrade that evidence.
    assert outcome.process_cleanup_confirmed is False
    assert "command cancellation requested" in outcome.stderr_path.read_text(encoding="utf-8")
    command_log = outcome.stdout_path.parent / "command.txt"
    assert "cancel_requested=true" in command_log.read_text(encoding="utf-8")


def test_normal_command_keeps_existing_non_cancelled_semantics(tmp_path):
    outcome = run_command(
        [sys.executable, "-c", "print('ok')"],
        tmp_path,
        tmp_path / "runs",
        5,
        cancellation_probe=Probe(),
    )
    assert outcome.ok is True
    assert outcome.cancel_requested is False
    assert outcome.timed_out is False
    assert outcome.process_cleanup_confirmed is None
