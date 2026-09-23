"""Filesystem tools. Read-only, sandboxed, line-addressable."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterator

from .tool_primitives import NotFoundError, Risk, ToolError, ToolRegistry, ToolResult, relpath, resolve_in_repo
from .tool_context import ToolContext

_SKIP_DIRS = {
    ".git", "build", "out", "node_modules", "__pycache__", ".venv",
    ".venv-workstation", ".local-agent", ".cache", ".pytest_cache", "dist",
    "cmake-build-debug", "cmake-build-release",
}
_MAX_RANGE_LINES = 400
_MAX_LIST_LIMIT = 10_000

_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl",
    ".txt", ".md", ".cmake", ".toml", ".yaml", ".yml", ".json", ".py", ".sh",
    ".ps1", ".ini", ".cfg", ".in", ".s", ".asm",
}


def _iter_files(base: Path, *, recursive: bool) -> Iterator[Path]:
    """Yield files deterministically while pruning excluded trees before descent."""
    if not recursive:
        for item in sorted(base.iterdir(), key=lambda value: value.name):
            if item.is_file():
                yield item
        return

    for root, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        # Mutating dirnames is the os.walk contract for preventing descent. Do this
        # before sorting/yielding any child so excluded trees never become traversal cost.
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        root_path = Path(root)
        for name in sorted(filenames):
            yield root_path / name


def _read_text_window(
    target: Path,
    *,
    start_line: int,
    end_line: int | None,
    max_read_bytes: int,
) -> tuple[list[str], int, int | None]:
    """Read a bounded line window without materialising a large file.

    Returns ``(window, effective_start, total_lines)``. ``total_lines`` is ``None``
    when an explicit range stopped before EOF; callers must not scan the remainder merely
    to compute presentation metadata.
    """
    if start_line < 1:
        raise ToolError("start_line must be at least 1")
    if end_line is not None:
        if end_line < start_line:
            raise ToolError("end_line must be greater than or equal to start_line")
        if end_line - start_line + 1 > _MAX_RANGE_LINES:
            raise ToolError(f"read range may contain at most {_MAX_RANGE_LINES} lines")

    size = target.stat().st_size
    if end_line is None and size > max_read_bytes:
        raise ToolError(
            f"{target.name!r} is {size} bytes, over the {max_read_bytes} byte whole-file "
            "read limit. Request an explicit line range."
        )

    window: list[str] = []
    encoded_bytes = 0
    last_seen = 0
    stopped_before_eof = False
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            last_seen = line_number
            if end_line is not None and line_number > end_line:
                stopped_before_eof = True
                break
            if line_number < start_line:
                continue
            line = raw.rstrip("\r\n")
            encoded_bytes += len(line.encode("utf-8"))
            if encoded_bytes > max_read_bytes:
                raise ToolError(
                    f"requested line range exceeds the {max_read_bytes} byte read limit"
                )
            window.append(line)

    total_lines = None if stopped_before_eof else last_seen
    return window, start_line, total_lines


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    @reg.add(
        "repo_info",
        "Report repository name, root, configured build profiles and the current "
        "permission policy. Call this first when you do not know how the "
        "repository is set up.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        Risk.READ,
    )
    def repo_info() -> ToolResult:
        pol = ctx.repo.policy
        return ToolResult(
            ok=True,
            summary=f"Repository {ctx.repo.name!r} with "
                    f"{len(ctx.repo.profiles)} build profile(s).",
            data={
                "name": ctx.repo.name,
                "root": str(ctx.root),
                "build_dir": ctx.repo.build_dir,
                "default_profile": ctx.repo.default_profile,
                "profiles": {
                    name: {
                        "configure": p.configure,
                        "build": p.build,
                        "test": p.test,
                    }
                    for name, p in ctx.repo.profiles.items()
                },
                "policy": {
                    "allow_build": pol.allow_build,
                    "allow_test": pol.allow_test,
                    "allow_patch": pol.allow_patch,
                    "allow_commit": pol.allow_commit,
                    "command_timeout_seconds": pol.command_timeout_seconds,
                },
            },
        )

    @reg.add(
        "list_files",
        "List files under a directory in the repository. Supports a glob pattern. "
        "Build output, managed environments, .git and vendored directories are pruned "
        "before recursive traversal.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory relative to repo root.", "default": "."},
                "pattern": {"type": "string", "description": "Glob such as *.cpp.", "default": "*"},
                "recursive": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 200},
            },
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def list_files(
        path: str = ".",
        pattern: str = "*",
        recursive: bool = True,
        limit: int = 200,
    ) -> ToolResult:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ToolError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
        base = resolve_in_repo(ctx.root, path)
        if not base.is_dir():
            raise NotFoundError(f"{path!r} is not a directory")

        found: list[str] = []
        truncated = False
        for item in _iter_files(base, recursive=recursive):
            if not fnmatch.fnmatch(item.name, pattern):
                continue
            if len(found) >= limit:
                truncated = True
                break
            found.append(relpath(ctx.root, item))

        return ToolResult(
            ok=True,
            summary=f"{len(found)} file(s) under {path!r} matching {pattern!r}"
                    + (" (truncated)" if truncated else ""),
            data={"files": found, "truncated": truncated},
        )

    @reg.add(
        "read_file",
        "Read a text file from the repository. Whole-file reads are byte-bounded; "
        "explicit line ranges stream at most 400 lines without materialising the rest.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "end_line": {"type": "integer", "description": "Inclusive. Omit for end of file."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> ToolResult:
        target = resolve_in_repo(ctx.root, path)
        if not target.is_file():
            raise NotFoundError(f"{path!r} is not a file")

        if target.suffix and target.suffix.lower() not in _TEXT_SUFFIXES:
            head = target.read_bytes()[:1024]
            if b"\x00" in head:
                raise ToolError(f"{path!r} looks like a binary file")

        window, start, total_lines = _read_text_window(
            target,
            start_line=start_line,
            end_line=end_line,
            max_read_bytes=ctx.repo.policy.max_read_bytes,
        )
        shown_end = start + len(window) - 1
        if total_lines is None:
            summary = f"{path} lines {start}-{shown_end} (bounded range)"
        else:
            summary = f"{path} lines {start}-{shown_end} of {total_lines}"

        return ToolResult(
            ok=True,
            summary=summary,
            data={
                "path": relpath(ctx.root, target),
                "total_lines": total_lines,
                "start_line": start,
                "content": "\n".join(
                    f"{start + i}: {ln}" for i, ln in enumerate(window)
                ),
            },
        )

    @reg.add(
        "submit_answer",
        "Finish the task. State your claim and cite the tool call ids that "
        "support it. Do NOT assert that something builds or passes: cite the "
        "build or test call and the orchestrator will decide whether the "
        "evidence supports the claim.",
        {
            "type": "object",
            "properties": {
                "claim": {
                    "type": "string",
                    "enum": ["success", "failure", "diagnosis", "needs_action"],
                    "description": "success = the thing now works; failure = it does "
                                   "not; diagnosis = here is the cause; needs_action "
                                   "= a human must decide.",
                },
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool call ids, as given back in each tool result, "
                                   "e.g. build_target:3.",
                },
                "summary": {"type": "string"},
            },
            "required": ["claim", "summary"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def submit_answer(claim: str, summary: str, evidence_ids: list[str] | None = None) -> ToolResult:
        # Handled specially by the orchestrator, which checks the cited evidence
        # against what actually ran. This body only validates the shape.
        return ToolResult(
            ok=True,
            summary=f"answer submitted ({claim})",
            data={"claim": claim, "summary": summary,
                  "evidence_ids": list(evidence_ids or [])},
        )

    @reg.add(
        "read_log_chunk",
        "Page through a captured run log by line range. Use this after a build or "
        "test tool points you at an artifact.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def read_log_chunk(path: str, start_line: int, end_line: int) -> ToolResult:
        from .logs import read_chunk

        target = resolve_in_repo(ctx.root, path)
        if not target.is_file():
            raise NotFoundError(f"{path!r} is not a file")
        if end_line - start_line > 400:
            end_line = start_line + 400
        chunk = read_chunk(target, start_line, end_line)
        return ToolResult(
            ok=True,
            summary=f"{path} lines {chunk['start_line']}-{chunk['end_line']}",
            data={"content": "\n".join(chunk["lines"])},
        )
