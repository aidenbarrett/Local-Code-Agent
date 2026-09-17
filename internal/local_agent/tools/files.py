"""Filesystem tools. Read-only, sandboxed, line-addressable."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from .base import NotFoundError, Risk, ToolError, ToolRegistry, ToolResult, relpath, resolve_in_repo
from .context import ToolContext

_SKIP_DIRS = {
    ".git", "build", "out", "node_modules", "__pycache__", ".venv",
    ".local-agent", ".cache", "cmake-build-debug", "cmake-build-release",
}

_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl",
    ".txt", ".md", ".cmake", ".toml", ".yaml", ".yml", ".json", ".py", ".sh",
    ".ps1", ".ini", ".cfg", ".in", ".s", ".asm",
}


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
        "Build output, .git and vendored directories are excluded.",
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
        base = resolve_in_repo(ctx.root, path)
        if not base.is_dir():
            raise NotFoundError(f"{path!r} is not a directory")

        found: list[str] = []
        truncated = False
        walker: Any = base.rglob("*") if recursive else base.glob("*")
        for item in sorted(walker):
            if any(part in _SKIP_DIRS for part in item.relative_to(base).parts):
                continue
            if not item.is_file():
                continue
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
        "Read a text file from the repository, optionally a line range. Always "
        "prefer a range over reading a whole large source file.",
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

        size = target.stat().st_size
        if size > ctx.repo.policy.max_read_bytes:
            raise ToolError(
                f"{path!r} is {size} bytes, over the {ctx.repo.policy.max_read_bytes} "
                "byte read limit. Request a line range."
            )
        if target.suffix and target.suffix.lower() not in _TEXT_SUFFIXES:
            head = target.read_bytes()[:1024]
            if b"\x00" in head:
                raise ToolError(f"{path!r} looks like a binary file")

        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = len(lines) if end_line is None else min(len(lines), end_line)
        window = lines[start - 1 : end]

        return ToolResult(
            ok=True,
            summary=f"{path} lines {start}-{start + len(window) - 1} of {len(lines)}",
            data={
                "path": relpath(ctx.root, target),
                "total_lines": len(lines),
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
