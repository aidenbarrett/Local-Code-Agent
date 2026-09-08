"""Git tools.

Read-only by default. The write side exists but is behind the policy engine,
and the destructive side does not exist as a tool at all. You cannot approve
your way into `push --force` because there is nothing to approve.
"""

from __future__ import annotations

import subprocess
from typing import Any

from .base import Risk, ToolError, ToolRegistry, ToolResult
from .context import ToolContext

_GIT_TIMEOUT = 120

# Anything not on this list is never handed to git, whatever the model asks for.
_ALLOWED_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "merge-base", "branch",
    "symbolic-ref", "add", "commit", "stash",
}


def _git(ctx: ToolContext, args: list[str]) -> tuple[int, str, str]:
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise ToolError(f"git subcommand {args[:1]} is not permitted by this agent")
    proc = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(ctx.root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_GIT_TIMEOUT,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_porcelain_v2(stdout: str) -> dict[str, Any]:
    branch: dict[str, str] = {}
    changed: list[dict[str, str]] = []
    untracked: list[str] = []

    for line in stdout.splitlines():
        if line.startswith("# branch."):
            key, _, value = line[len("# branch.") :].partition(" ")
            branch[key] = value
        elif line.startswith("1 ") or line.startswith("2 "):
            fields = line.split(" ", 8)
            xy = fields[1]
            path = fields[-1]
            changed.append(
                {
                    "path": path.split("\t")[0],
                    "staged": xy[0] if xy[0] != "." else "",
                    "worktree": xy[1] if xy[1] != "." else "",
                }
            )
        elif line.startswith("? "):
            untracked.append(line[2:])

    return {"branch": branch, "changed": changed, "untracked": untracked}


def register(reg: ToolRegistry, ctx: ToolContext, journal: object | None = None) -> None:
    @reg.add(
        "git_status",
        "Show the working tree status: current branch, upstream, staged and "
        "unstaged changes, untracked files.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        Risk.READ,
    )
    def git_status() -> ToolResult:
        code, out, err = _git(ctx, ["status", "--porcelain=v2", "--branch"])
        if code != 0:
            raise ToolError(f"git status failed: {err.strip()}")
        parsed = _parse_porcelain_v2(out)
        n = len(parsed["changed"])
        u = len(parsed["untracked"])
        return ToolResult(
            ok=True,
            summary=f"{n} changed file(s), {u} untracked, on "
                    f"{parsed['branch'].get('head', 'unknown')}",
            data=parsed,
        )

    @reg.add(
        "git_diff",
        "Show the diff of the working tree, or of the staging area with staged=true. "
        "Restrict to a path when the change set is large.",
        {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "default": False},
                "path": {"type": "string"},
                "context_lines": {"type": "integer", "default": 3},
                "stat_only": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def git_diff(
        staged: bool = False,
        path: str | None = None,
        context_lines: int = 3,
        stat_only: bool = False,
    ) -> ToolResult:
        args = ["diff", "--no-ext-diff", f"--unified={max(0, min(context_lines, 12))}"]
        if staged:
            args.append("--cached")
        if stat_only:
            args.append("--stat")
        if path:
            args += ["--", path]
        code, out, err = _git(ctx, args)
        if code != 0:
            raise ToolError(f"git diff failed: {err.strip()}")

        files = [ln.split(" b/")[-1] for ln in out.splitlines() if ln.startswith("diff --git ")]
        return ToolResult(
            ok=True,
            summary=f"diff over {len(files)} file(s)"
                    + (" (staged)" if staged else "")
                    + (" [stat only]" if stat_only else ""),
            data={"files": files, "diff": out if len(out) < 60_000 else out[:60_000]},
        )

    @reg.add(
        "git_log",
        "Show recent commits, one line each, optionally for a single path.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 15},
                "path": {"type": "string"},
            },
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def git_log(limit: int = 15, path: str | None = None) -> ToolResult:
        args = ["log", f"-{max(1, min(limit, 100))}", "--date=short",
                "--pretty=format:%h|%ad|%an|%s"]
        if path:
            args += ["--", path]
        code, out, err = _git(ctx, args)
        if code != 0:
            raise ToolError(f"git log failed: {err.strip()}")
        commits = []
        for ln in out.splitlines():
            bits = ln.split("|", 3)
            if len(bits) == 4:
                commits.append(
                    {"sha": bits[0], "date": bits[1], "author": bits[2], "subject": bits[3]}
                )
        return ToolResult(
            ok=True, summary=f"{len(commits)} commit(s)", data={"commits": commits}
        )

    @reg.add(
        "git_show",
        "Show a single commit: metadata and diff.",
        {
            "type": "object",
            "properties": {
                "rev": {"type": "string", "default": "HEAD"},
                "stat_only": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def git_show(rev: str = "HEAD", stat_only: bool = True) -> ToolResult:
        if any(ch in rev for ch in " ;|&$`\n"):
            raise ToolError("suspicious revision string")
        args = ["show", "--no-ext-diff", rev]
        if stat_only:
            args.append("--stat")
        code, out, err = _git(ctx, args)
        if code != 0:
            raise ToolError(f"git show failed: {err.strip()}")
        return ToolResult(ok=True, summary=f"commit {rev}", data={"text": out[:40_000]})

    @reg.add(
        "git_branch_info",
        "Show the current branch, its upstream, and the merge base against a base "
        "branch. Use this to work out what 'my changes' actually means.",
        {
            "type": "object",
            "properties": {"base": {"type": "string", "default": "origin/main"}},
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def git_branch_info(base: str = "origin/main") -> ToolResult:
        code, head, _ = _git(ctx, ["rev-parse", "--abbrev-ref", "HEAD"])
        if code != 0:
            raise ToolError("not a git repository")
        data: dict[str, Any] = {"head": head.strip()}
        mb_code, mb, _ = _git(ctx, ["merge-base", base, "HEAD"])
        data["base"] = base
        data["merge_base"] = mb.strip() if mb_code == 0 else None
        if mb_code != 0:
            data["note"] = f"{base!r} is not resolvable in this repository"
        return ToolResult(ok=True, summary=f"on {data['head']}", data=data)

    # ---------------------------------------------------------------- writes

    @reg.add(
        "git_stage",
        "Stage specific paths. Requires approval. Never stages everything blindly: "
        "name the files.",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        Risk.DANGEROUS,
    )
    def git_stage(paths: list[str]) -> ToolResult:
        if not paths:
            raise ToolError("name at least one path")
        for p in paths:
            if p in (".", "-A", "--all", "*"):
                raise ToolError("blanket staging is not permitted; name the files")
        code, _, err = _git(ctx, ["add", "--", *paths])
        if code != 0:
            raise ToolError(f"git add failed: {err.strip()}")
        return ToolResult(ok=True, summary=f"staged {len(paths)} path(s)",
                          data={"paths": paths})

    @reg.add(
        "git_commit",
        "Create a commit from what is already staged. Requires approval. Does not "
        "stage anything itself and never pushes.",
        {
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 8}},
            "required": ["message"],
            "additionalProperties": False,
        },
        Risk.DANGEROUS,
    )
    def git_commit(message: str) -> ToolResult:
        code, staged, _ = _git(ctx, ["diff", "--cached", "--name-only"])
        if code != 0 or not staged.strip():
            raise ToolError("nothing staged; stage explicit paths first")
        code, out, err = _git(ctx, ["commit", "-m", message])
        if code != 0:
            raise ToolError(f"git commit failed: {err.strip() or out.strip()}")
        if journal is not None:
            journal.record_irreversible(f"commit created: {message.splitlines()[0][:80]}")
        return ToolResult(
            ok=True,
            summary="commit created",
            data={"files": staged.split(), "output": out.strip()[:2000]},
        )
