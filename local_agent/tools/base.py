"""Tool primitives: results, registry, path sandboxing.

Every tool returns a `ToolResult`. The model never sees a raw 48 MB build log;
it sees a small structured summary plus the path of an artefact it can page
through with `read_log_chunk`. Progressive disclosure applied to output, not
just to skills.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Risk(str, Enum):
    """What a tool is allowed to do without a human in the loop."""

    READ = "read"          # never mutates anything
    WRITE = "write"        # mutates the worktree
    EXECUTE = "execute"    # runs a configured command
    DANGEROUS = "dangerous"  # can lose work; requires approval, always
    FORBIDDEN = "forbidden"  # never, under any configuration


class ExecutionStatus(str, Enum):
    """Did the tool run at all? This axis is about infrastructure."""

    OK = "ok"            # the tool executed and produced a real result
    BLOCKED = "blocked"  # the environment prevented it running
    ERROR = "error"      # the tool ran but could not complete


class DomainStatus(str, Enum):
    """Did the thing being examined succeed? This axis is about the code."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Locus(str, Enum):
    """Where a failure lives. Derived from Reason, never chosen separately.

    This is what lets a report say "model failures: 3, environment: 2" without
    anybody reading a summary string.
    """

    MODEL = "model"              # the model asked for something wrong
    SERVER = "server"            # the inference server is gone or broken
    TOOL = "tool"                # our own tool machinery failed
    ENVIRONMENT = "environment"  # the machine would not let the tool run
    POLICY = "policy"            # we forbade it
    USER = "user"                # the operator declined


class Reason(str, Enum):
    """Why an execution was blocked or errored. Never inferred from prose.

    Every member has a producer somewhere in src/. A member nobody constructs
    is a promise the code does not keep, so do not add one speculatively.
    """

    POLICY_DENIED = "policy_denied"
    APPROVAL_DECLINED = "approval_declined"
    MISSING_EXECUTABLE = "missing_executable"
    SPAWN_FAILURE = "spawn_failure"
    ORCHESTRATOR_TIMEOUT = "orchestrator_timeout"  # OUR wall clock killed it
    BAD_ARGUMENTS = "bad_arguments"
    SANDBOX_VIOLATION = "sandbox_violation"
    UNKNOWN_TOOL = "unknown_tool"        # no such tool anywhere: the model invented it
    TOOL_NOT_ALLOWED = "tool_not_allowed"  # a real tool, not offered by the active skill
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"
    SERVER_UNAVAILABLE = "server_unavailable"  # could not reach it, or it died
    SERVER_STALLED = "server_stalled"          # alive, answering, made no progress
    # Three ways a test run executes perfectly and still tells you nothing
    # about the tree in front of you. ctest does not compile, so all three are
    # statements about which binaries were on disk, not about the source.
    STALE_BINARY = "stale_binary"          # sources newer than the last full build
    NO_BUILD_RECORD = "no_build_record"    # no successful full build recorded at all
    PROFILE_MISMATCH = "profile_mismatch"  # built or configured for another profile
    # A write aimed at the instrument rather than at the code: the build
    # directory, the run journal, or .git internals. Refused, and recorded,
    # because attempting it is itself a measurement.
    PROTECTED_PATH = "protected_path"

    @property
    def locus(self) -> Locus:
        return _LOCUS[self]


_LOCUS: dict[Reason, Locus] = {
    Reason.BAD_ARGUMENTS: Locus.MODEL,
    Reason.UNKNOWN_TOOL: Locus.MODEL,
    Reason.TOOL_NOT_ALLOWED: Locus.MODEL,
    Reason.SERVER_UNAVAILABLE: Locus.SERVER,
    Reason.SERVER_STALLED: Locus.SERVER,
    Reason.SPAWN_FAILURE: Locus.TOOL,
    Reason.ORCHESTRATOR_TIMEOUT: Locus.TOOL,
    Reason.INTERNAL_ERROR: Locus.TOOL,
    Reason.MISSING_EXECUTABLE: Locus.ENVIRONMENT,
    Reason.SANDBOX_VIOLATION: Locus.ENVIRONMENT,
    Reason.NOT_FOUND: Locus.MODEL,
    Reason.POLICY_DENIED: Locus.POLICY,
    Reason.APPROVAL_DECLINED: Locus.USER,
    # MODEL, because the model is the one that can fix all three, by building
    # the profile it wants to test before testing it.
    Reason.STALE_BINARY: Locus.MODEL,
    Reason.NO_BUILD_RECORD: Locus.MODEL,
    Reason.PROFILE_MISMATCH: Locus.MODEL,
    Reason.PROTECTED_PATH: Locus.MODEL,
}
assert set(_LOCUS) == set(Reason), "every Reason needs a Locus"


class ToolError(RuntimeError):
    """A condition the model is allowed to see and fix. execution_status ERROR."""

    reason: Reason = Reason.BAD_ARGUMENTS

    def __init__(self, message: str, reason: Reason | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class NotFoundError(ToolError):
    """The thing the model asked for is not there. Its problem to fix, so ERROR."""

    reason = Reason.NOT_FOUND


class SandboxError(ToolError):
    """Raised when a path escapes the repository root."""

    reason = Reason.SANDBOX_VIOLATION


class BlockedError(ToolError):
    """The environment prevented the tool from running at all.

    Policy forbade it, the executable is missing, the operator declined, our own
    wall clock expired. Never a statement about whether the model was right.
    """

    reason = Reason.POLICY_DENIED

    def __init__(self, message: str, reason: Reason = Reason.POLICY_DENIED) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class ToolResult:
    """Two independent axes, because conflating them was a real bug.

        gcc returns a compile error   execution OK, domain FAIL   (valid evidence)
        cmake is not installed        execution BLOCKED, domain UNKNOWN
        ctest reports a test TIMEOUT  execution OK, domain FAIL   (valid evidence)
        our runner killed ctest       execution ERROR, domain UNKNOWN,
                                      reason ORCHESTRATOR_TIMEOUT

    `ok` is kept as a convenience for "the thing under examination succeeded",
    which is domain PASS with a clean execution. Nothing classifies outcomes by
    reading English out of `summary` any more.
    """

    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    exit_code: int | None = None

    execution_status: ExecutionStatus = ExecutionStatus.OK
    # None means "not stated, infer it from ok". UNKNOWN means UNKNOWN and is
    # left alone: a tool that ran cleanly and learned nothing has to be able to
    # say so. `run_test` with a filter matching no test is the case that forced
    # the distinction. It exits 0 having verified nothing, and inferring PASS
    # from ok, or FAIL from not-ok, would both be inventing an observation.
    domain_status: DomainStatus | None = None
    reason: Reason | None = None

    def __post_init__(self) -> None:
        # Infer the domain axis for the many tools that only set `ok`.
        if self.domain_status is None:
            self.domain_status = (
                (DomainStatus.PASS if self.ok else DomainStatus.FAIL)
                if self.execution_status is ExecutionStatus.OK
                else DomainStatus.UNKNOWN
            )

    @property
    def blocked(self) -> bool:
        """The environment stopped it. Never counted as a model failure."""
        return self.execution_status is ExecutionStatus.BLOCKED

    @property
    def ran(self) -> bool:
        """Did the tool execute cleanly, whatever it then found?"""
        return self.execution_status is ExecutionStatus.OK

    @property
    def observed(self) -> bool:
        """Did this produce a real observation, whatever the observation said?

        Executing is not observing. `run_test` with a filter matching no test
        exits 0 having exercised nothing: execution OK, domain UNKNOWN. That
        must not count as having reproduced or verified anything, and `ran`
        cannot tell the difference because it only looks at the execution axis.
        """
        return (
            self.execution_status is ExecutionStatus.OK
            and self.domain_status in (DomainStatus.PASS, DomainStatus.FAIL)
        )

    @staticmethod
    def blocked_by(reason: Reason, summary: str, **kwargs: Any) -> "ToolResult":
        return ToolResult(
            ok=False,
            summary=summary,
            execution_status=ExecutionStatus.BLOCKED,
            domain_status=DomainStatus.UNKNOWN,
            reason=reason,
            **kwargs,
        )

    @staticmethod
    def errored(reason: Reason, summary: str, **kwargs: Any) -> "ToolResult":
        return ToolResult(
            ok=False,
            summary=summary,
            execution_status=ExecutionStatus.ERROR,
            domain_status=DomainStatus.UNKNOWN,
            reason=reason,
            **kwargs,
        )

    def to_json(self, max_bytes: int = 16_000) -> str:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "summary": self.summary,
            "execution": self.execution_status.value,
            "result": self.domain_status.value,
        }
        if self.reason is not None:
            payload["reason"] = self.reason.value
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.artifacts:
            payload["artifacts"] = self.artifacts
        if self.data:
            payload["data"] = self.data

        text = json.dumps(payload, indent=2, default=str)
        if len(text.encode("utf-8")) <= max_bytes:
            return text

        # Too big. Drop the payload, keep the handle. The model can page.
        trimmed = {
            "ok": self.ok,
            "summary": self.summary,
            "truncated": True,
            "note": (
                "Result exceeded the tool-result budget. Use read_log_chunk or "
                "read_file against the listed artifacts to page through it."
            ),
            "artifacts": self.artifacts,
        }
        if self.exit_code is not None:
            trimmed["exit_code"] = self.exit_code
        return json.dumps(trimmed, indent=2, default=str)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult]
    risk: Risk

    def as_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name!r}")
        self._tools[tool.name] = tool

    def add(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        risk: Risk,
    ) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
        def decorate(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
            self.register(Tool(name, description, parameters, fn, risk))
            return fn

        return decorate

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolError(
                f"unknown tool {name!r}; available: {sorted(self._tools)}",
                Reason.UNKNOWN_TOOL,
            )
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Only ever expose the toolset the current skill needs.

        An 8B model shown thirty tools has thirty opportunities to do something
        stupid.
        """
        chosen = names if names is not None else self.names()
        return [self.get(n).as_openai_schema() for n in chosen if n in self._tools]


# --------------------------------------------------------------------------
# path sandbox
# --------------------------------------------------------------------------


def resolve_in_repo(root: Path, candidate: str | os.PathLike[str]) -> Path:
    """Resolve `candidate` and prove it lies beneath `root`.

    Blocks `../../../../.ssh/id_rsa`, absolute escapes, and symlink escapes,
    because `Path.resolve()` follows symlinks before the containment check.
    """
    root_resolved = root.resolve()
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else (root_resolved / raw)
    resolved = joined.resolve()

    if resolved == root_resolved:
        return resolved
    if root_resolved not in resolved.parents:
        raise SandboxError(
            f"path {str(candidate)!r} resolves outside the repository root"
        )
    return resolved


class ProtectedPathError(ToolError):
    """A write aimed at the instrument instead of at the code under test."""

    reason = Reason.PROTECTED_PATH


def assert_writable(root: Path, target: Path, protected: tuple[str, ...]) -> Path:
    """Refuse a write that lands on the instrument rather than on the source.

    `resolve_in_repo` proves a path is inside the repository. That is not the
    same as proving it is a legitimate target: the build directory is inside
    the repository too, and so is the run journal.

    This exists because the build stamp was reachable. `run_test` decides
    whether a result is stale by comparing source mtimes against the recorded
    build, and the recorded build lives at `build/.local-agent-build-ok`, an
    ordinary file that `propose_patch` was happy to rewrite. Three calls,
    nothing compiled, and the staleness gate opened:

        propose_patch(path="build/.local-agent-build-ok", find=..., replace=...)
        apply_patch(...)
        run_test()          -> stale_sources [], domain PASS, verified

    Worse than an ordinary cheat, because the tool that makes it possible is
    only offered in some conditions. The control condition holds the full
    registry on every case, including the read-only diagnosis cases where the
    narrow and skill conditions have no patch tools at all, so a cheat that
    needs `apply_patch` is available to one arm of the experiment and not the
    others. That does not add noise to the contrast; it biases it.
    """
    resolved = target.resolve()
    root_resolved = root.resolve()
    for name in protected:
        if not name:
            continue
        guard = (root_resolved / name).resolve()
        if resolved == guard or guard in resolved.parents:
            raise ProtectedPathError(
                f"{relpath(root, target)!r} is inside {name!r}, which belongs to "
                "the build system and the agent, not to the project. Edit the "
                "source and rebuild; changing what records the build does not "
                "change what was built."
            )
    return resolved


def relpath(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
