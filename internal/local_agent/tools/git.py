"""Git tools.

Read-only by default. The write side exists but is behind the policy engine,
and the destructive side does not exist as a tool at all. You cannot approve
your way into `push --force` because there is nothing to approve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .tool_primitives import Risk, ToolError, ToolRegistry, ToolResult
from .tool_context import ToolContext

_GIT_TIMEOUT = 120

# Anything not on this list is never handed to git, whatever the model asks for.
_ALLOWED_SUBCOMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "merge-base", "branch",
    "symbolic-ref", "add", "commit", "stash",
}


def _no_hooks_dir(ctx: ToolContext) -> Path:
    """An empty directory to point ``core.hooksPath`` at, so repository hooks never run.

    A hook is code from the repository being worked on. Running it would let repository
    content execute with the agent's authority, the same rule the Session Hub's own
    commit path (session/workspaces.py) enforces.
    """
    path = ctx.run_root / ".no-hooks"
    path.mkdir(exist_ok=True)
    return path


def _git(
    ctx: ToolContext, args: list[str], *, literal_paths: bool = False,
) -> tuple[int, str, str]:
    if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
        raise ToolError(f"git subcommand {args[:1]} is not permitted by this agent")
    proc = subprocess.run(
        [
            "git",
            *(["--literal-pathspecs"] if literal_paths else []),
            "-c", f"core.hooksPath={_no_hooks_dir(ctx)}",
            "-c", "core.fsmonitor=false",
            "-c", "diff.external=",
            "--no-pager",
            *args,
        ],
        cwd=str(ctx.root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_GIT_TIMEOUT,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_commit(ctx: ToolContext, value: str, *, label: str) -> str | None:
    """Resolve untrusted revision text before passing it to another git command.

    A READ-risk tool must stay read-only even when git itself accepts effectful
    options such as ``--output=<path>`` or ``--ext-diff``.  Shell metacharacter
    filtering does not help because git receives an argv vector directly.  Refuse
    option-shaped input, resolve only through ``rev-parse --end-of-options``, then
    hand downstream commands the resulting hexadecimal object id rather than the
    original model-controlled string.
    """
    if not value or value.startswith("-") or "\x00" in value or "\n" in value or "\r" in value:
        raise ToolError(f"suspicious {label}: revision-like values may not be options")
    code, out, _ = _git(
        ctx,
        ["rev-parse", "--verify", "--end-of-options", f"{value}^{{commit}}"],
    )
    if code != 0:
        return None
    resolved = out.strip()
    if not resolved or any(ch not in "0123456789abcdefABCDEF" for ch in resolved):
        raise ToolError(f"git returned an invalid object id while resolving {label}")
    return resolved


# Unmerged index states from ``git status --porcelain=v2``, in git's own terms.
_CONFLICT_STATES = {
    "DD": "both deleted",
    "AU": "added by us",
    "UD": "deleted by them",
    "UA": "added by them",
    "DU": "deleted by us",
    "AA": "both added",
    "UU": "both modified",
}

# Marker files under the git directory, checked in this order. A rebase stopped
# on a conflict can leave single-commit head files beside its own directory, so
# the rebase directories are checked before the single-commit heads.
_OPERATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply/applying", "am"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


# What the user runs to finish or back out of each stopped operation. These tools
# never run them: the commands are reported so advice comes from git's real state
# rather than from whatever the model remembers about each operation.
_OPERATION_COMMANDS: dict[str, dict[str, str]] = {
    op: {"continue": f"git {op} --continue", "abort": f"git {op} --abort"}
    for op in ("merge", "rebase", "am", "cherry-pick", "revert")
}


def _operation_in_progress(ctx: ToolContext) -> dict[str, Any]:
    """Report the interrupted multi-step operation, if any, from git's own markers.

    Bisect is reported separately: it is a search over history rather than a
    half-finished change, and it does not block committing.
    """
    names = [marker for marker, _ in _OPERATION_MARKERS] + ["BISECT_LOG"]
    code, out, err = _git(ctx, ["rev-parse", *(arg for n in names for arg in ("--git-path", n))])
    if code != 0:
        raise ToolError(f"git rev-parse --git-path failed: {err.strip()}")
    paths = out.splitlines()
    if len(paths) != len(names):
        raise ToolError("git rev-parse --git-path returned an unexpected number of paths")
    present = {name: (ctx.root / path).exists() for name, path in zip(names, paths, strict=True)}
    operation = next((op for marker, op in _OPERATION_MARKERS if present[marker]), None)
    return {
        "operation": operation,
        "operation_commands": _OPERATION_COMMANDS[operation] if operation else None,
        "bisecting": present["BISECT_LOG"],
    }


def _parse_porcelain_v2(stdout: str) -> dict[str, Any]:
    branch: dict[str, str] = {}
    changed: list[dict[str, str]] = []
    conflicted: list[dict[str, str]] = []
    untracked: list[str] = []

    records = iter(stdout.split("\0"))
    for line in records:
        if line.startswith("# branch."):
            key, _, value = line[len("# branch.") :].partition(" ")
            branch[key] = value
        elif line.startswith("1 ") or line.startswith("2 "):
            renamed = line.startswith("2 ")
            fields = line.split(" ", 9 if renamed else 8)
            xy = fields[1]
            item = {
                "path": fields[-1],
                "staged": xy[0] if xy[0] != "." else "",
                "worktree": xy[1] if xy[1] != "." else "",
            }
            if renamed:
                item["original_path"] = next(records)
            changed.append(item)
        elif line.startswith("u "):
            fields = line.split(" ", 10)
            xy = fields[1]
            conflicted.append(
                {"path": fields[-1], "state": _CONFLICT_STATES.get(xy, f"unmerged {xy}")}
            )
        elif line.startswith("? "):
            untracked.append(line[2:])

    ahead_behind = branch.get("ab", "").split()
    divergence: dict[str, int] | None = None
    if len(ahead_behind) == 2 and ahead_behind[0][:1] == "+" and ahead_behind[1][:1] == "-":
        divergence = {"ahead": int(ahead_behind[0][1:]), "behind": int(ahead_behind[1][1:])}
    return {
        "branch": branch,
        "upstream_divergence": divergence,
        "changed": changed,
        "conflicted": conflicted,
        "untracked": untracked,
    }


def _name_status(stdout: str) -> list[dict[str, str]]:
    """Parse ``git diff --name-status -z``: renames and copies carry both paths."""
    records = iter(stdout.split("\0"))
    files: list[dict[str, str]] = []
    for status in records:
        if not status:
            continue
        if status[0] in "RC":
            original, path = next(records), next(records)
            files.append({"status": status[0], "path": path, "original_path": original})
        else:
            files.append({"status": status[0], "path": next(records)})
    return files


def register(reg: ToolRegistry, ctx: ToolContext, journal: object | None = None) -> None:
    @reg.add(
        "git_status",
        "Show the working tree status: current branch, how far it is ahead of and "
        "behind its upstream, staged and unstaged changes, untracked files, "
        "conflicted files, and any merge, rebase, cherry-pick or revert that has "
        "stopped part-way.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        Risk.READ,
    )
    def git_status() -> ToolResult:
        code, out, err = _git(ctx, ["status", "--porcelain=v2", "--branch", "-z"])
        if code != 0:
            raise ToolError(f"git status failed: {err.strip()}")
        parsed = _parse_porcelain_v2(out)
        parsed.update(_operation_in_progress(ctx))
        parts = [
            f"{len(parsed['changed'])} changed file(s)",
            f"{len(parsed['untracked'])} untracked",
        ]
        if parsed["conflicted"]:
            parts.append(f"{len(parsed['conflicted'])} conflicted")
        summary = ", ".join(parts) + f", on {parsed['branch'].get('head', 'unknown')}"
        divergence = parsed["upstream_divergence"]
        if divergence is not None and (divergence["ahead"] or divergence["behind"]):
            summary += f" ({divergence['ahead']} ahead, {divergence['behind']} behind upstream)"
        if parsed["operation"] is not None:
            summary += f"; a {parsed['operation']} is in progress"
        if parsed["bisecting"]:
            summary += "; a bisect is in progress"
        return ToolResult(ok=True, summary=summary, data=parsed)

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
        args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"--unified={max(0, min(context_lines, 12))}",
        ]
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
        resolved = _resolve_commit(ctx, rev, label="revision")
        if resolved is None:
            raise ToolError(f"revision {rev!r} is not resolvable in this repository")
        args = ["show", "--no-ext-diff", "--no-textconv"]
        if stat_only:
            args.append("--stat")
        args.append(resolved)
        code, out, err = _git(ctx, args)
        if code != 0:
            raise ToolError(f"git show failed: {err.strip()}")
        return ToolResult(ok=True, summary=f"commit {rev}", data={"text": out[:40_000]})

    @reg.add(
        "git_branch_info",
        "Show the current branch (or that HEAD is detached), its upstream, the merge "
        "base against a base branch, and the files changed since that merge base. Use "
        "this to work out what 'my changes' actually means.",
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
        name = head.strip()
        detached = name == "HEAD"
        commit_code, commit, _ = _git(ctx, ["rev-parse", "--verify", "HEAD"])
        data: dict[str, Any] = {
            "head": None if detached else name,
            "detached": detached,
            "head_commit": commit.strip() if commit_code == 0 else None,
            "upstream": None,
        }
        notes: list[str] = []
        if detached:
            notes.append("HEAD is detached; there is no current branch or upstream")
        else:
            up_code, upstream, _ = _git(
                ctx, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
            )
            if up_code == 0 and upstream.strip():
                data["upstream"] = upstream.strip()
            else:
                notes.append(f"branch {name!r} has no upstream configured")
        data["base"] = base
        data["merge_base"] = None
        data["files_changed"] = None
        resolved_base = _resolve_commit(ctx, base, label="base revision")
        if resolved_base is None:
            notes.append(f"{base!r} is not resolvable in this repository")
        else:
            mb_code, mb, _ = _git(ctx, ["merge-base", resolved_base, "HEAD"])
            if mb_code != 0:
                notes.append(f"{base!r} has no merge base with HEAD")
            else:
                merge_base = mb.strip()
                data["merge_base"] = merge_base
                diff_code, diff, err = _git(
                    ctx, ["diff", "--name-status", "-z", "--find-renames", merge_base, "HEAD"]
                )
                if diff_code != 0:
                    raise ToolError(f"git diff against the merge base failed: {err.strip()}")
                data["files_changed"] = _name_status(diff)
        if notes:
            data["note"] = "; ".join(notes)
        where = f"detached at {str(data['head_commit'])[:12]}" if detached else f"on {name}"
        return ToolResult(ok=True, summary=where, data=data)

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
        # Named files only: a name like `note[1].txt` must not also stage `note1.txt`.
        code, _, err = _git(ctx, ["add", "--", *paths], literal_paths=True)
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
        code, out, err = _git(ctx, ["commit", "--no-verify", "-m", message])
        if code != 0:
            raise ToolError(f"git commit failed: {err.strip() or out.strip()}")
        if journal is not None:
            journal.record_irreversible(f"commit created: {message.splitlines()[0][:80]}")
        return ToolResult(
            ok=True,
            summary="commit created",
            data={"files": staged.split(), "output": out.strip()[:2000]},
        )
