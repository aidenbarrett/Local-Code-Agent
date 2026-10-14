"""Shared state handed to every tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import BuildProfile, ConfigError, RepoConfig
from .tool_primitives import Reason, ToolError
from .process_runner import (
    CancellationProbe,
    CommandCancellationRequested,
    RunOutcome,
    run_command,
)


INSTRUMENT_DIR = ".local-agent"


def ignore_owned_output(root: Path, tree: Path) -> None:
    """Keep one of Local Code Agent's output trees out of the user's Git status.

    When ``tree`` (the run or build directory LCA writes) lies under
    ``<root>/.local-agent``, it gets a ``.gitignore`` of ``*`` (which also ignores
    itself), so its logs and build output never show as untracked work and the user's
    own ``.gitignore`` is never edited. Only that tree is ignored: anything else under
    ``.local-agent`` stays visible to Git. An existing file there is the user's and is
    left alone.
    """
    instrument = (root / INSTRUMENT_DIR).resolve()
    resolved = tree.resolve()
    if resolved != instrument and instrument not in resolved.parents:
        return
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        with (resolved / ".gitignore").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("# Created by Local Code Agent: nothing here is source.\n*\n")
    except FileExistsError:
        pass


@dataclass
class ToolContext:
    repo: RepoConfig
    # The admitted task's cancellation token. None outside a cancellable durable task,
    # in which case configured commands run exactly as before.
    cancellation_probe: CancellationProbe | None = None
    # How many configured commands actually started a process through this context.
    # Durable activity compares it around one tool call, so a call refused before
    # anything spawned is not mistaken for one that may have left a process behind.
    processes_started: int = 0

    def profile(self, name: str | None = None) -> BuildProfile:
        """Resolve a tool argument without exposing configuration errors as crashes."""
        try:
            return self.repo.profile(name)
        except ConfigError as exc:
            raise ToolError(str(exc), reason=Reason.BAD_ARGUMENTS) from exc

    @property
    def root(self) -> Path:
        return self.repo.root

    @property
    def run_root(self) -> Path:
        path = self.repo.run_path.resolve()
        if self.root.resolve() not in path.parents:
            raise ToolError("configured run directory is outside the repository")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def timeout(self) -> int:
        return self.repo.policy.command_timeout_seconds

    def _note_process_started(self) -> None:
        self.processes_started += 1

    def refuse_if_cancelled(self) -> None:
        """Fence controller-owned filesystem effects before they begin."""
        if self.cancellation_probe is None:
            return
        requested = self.cancellation_probe.requested
        if not isinstance(requested, bool):
            raise TypeError("cancellation probe requested state must be boolean")
        if requested:
            from .tool_primitives import BlockedError, Reason
            raise BlockedError(
                "Stop was requested before this filesystem effect; nothing changed.",
                Reason.COMMAND_CANCELLED,
            )

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

        # A configured command leaves logs here; read-only tools must leave no files,
        # and a Stop already requested fences this effect like every other.
        self.refuse_if_cancelled()
        ignore_owned_output(self.root, self.run_root)
        try:
            outcome = run_command(
                command,
                self.root,
                self.run_root,
                self.timeout,
                env,
                cancellation_probe=self.cancellation_probe,
                on_spawn=self._note_process_started,
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
