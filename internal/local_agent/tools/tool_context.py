"""Shared state handed to every tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import RepoConfig
from .process_runner import (
    CancellationProbe,
    CommandCancellationRequested,
    RunOutcome,
    run_command,
)


@dataclass
class ToolContext:
    repo: RepoConfig
    # The admitted task's cancellation token. None outside a cancellable durable task,
    # in which case configured commands run exactly as before.
    cancellation_probe: CancellationProbe | None = None

    @property
    def root(self) -> Path:
        return self.repo.root

    @property
    def run_root(self) -> Path:
        path = self.repo.run_path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def timeout(self) -> int:
        return self.repo.policy.command_timeout_seconds

    def run_configured(
        self, command: list[str], env: dict[str, str] | None = None
    ) -> RunOutcome:
        """Run one configured command under this task's timeout and Stop authority.

        A Stop observed before spawn raises ``BlockedError``: nothing ran. A Stop observed
        while the command ran ends its tree and raises ``ToolError``: the command was
        interrupted and its partial output is not evidence about the code. Both carry
        ``COMMAND_CANCELLED`` so the durable finish records ``cancelled`` rather than a
        build or test verdict.
        """
        from .tool_primitives import BlockedError, Reason, ToolError

        try:
            outcome = run_command(
                command,
                self.root,
                self.run_root,
                self.timeout,
                env,
                cancellation_probe=self.cancellation_probe,
            )
        except CommandCancellationRequested as exc:
            raise BlockedError(
                "Stop was requested before this command started; it did not run.",
                Reason.COMMAND_CANCELLED,
            ) from exc
        if outcome.cancel_requested:
            confirmed = outcome.process_cleanup_confirmed
            raise ToolError(
                "Stop interrupted this command; its output is not a result. "
                f"Process-tree cleanup confirmed={str(confirmed).lower()} "
                f"(containment={outcome.containment}).",
                Reason.COMMAND_CANCELLED,
            )
        return outcome
