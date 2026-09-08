"""Deterministic repository search.

No vector database on day one. A C++ repository already carries excellent
structure: names, types, symbols, includes, tests, paths, git history and
compiler diagnostics. Exhaust that before reaching for embeddings.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .base import Risk, ToolError, ToolRegistry, ToolResult, relpath, resolve_in_repo
from .context import ToolContext

_EXCLUDES = [
    "!build/", "!out/", "!.git/", "!node_modules/", "!.local-agent/",
    "!cmake-build-*/",
]

# Rough C++ definition shapes. Deliberately crude; it feeds `read_file`, it does
# not pretend to be a compiler front end.
_DEF_TEMPLATES = (
    r"(class|struct|enum class|enum|union)\s+{sym}\b",
    r"\b{sym}\s*\([^;]*\)\s*(const)?\s*(noexcept)?\s*\{{",
    r"\b{sym}\s*=",
    r"#define\s+{sym}\b",
    r"using\s+{sym}\s*=",
    r"namespace\s+{sym}\b",
)


def _rg_available() -> bool:
    return shutil.which("rg") is not None


def _run_rg(args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        ["rg", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def _run_git_grep(args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "grep", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout


def _parse_matches(stdout: str, limit: int) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for raw in stdout.splitlines():
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        path, line_no, text = parts
        if not line_no.isdigit():
            continue
        out.append(
            {"file": path, "line": int(line_no), "text": text.strip()[:240]}
        )
        if len(out) >= limit:
            break
    return out


def register(reg: ToolRegistry, ctx: ToolContext) -> None:
    @reg.add(
        "search_text",
        "Search the repository for a regular expression. Returns file, line and "
        "the matching text. Use this to locate code before reading it.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "glob": {"type": "string", "description": "Restrict to a glob such as *.cpp."},
                "path": {"type": "string", "default": "."},
                "case_sensitive": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 60},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def search_text(
        pattern: str,
        glob: str | None = None,
        path: str = ".",
        case_sensitive: bool = True,
        limit: int = 60,
    ) -> ToolResult:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc

        base = resolve_in_repo(ctx.root, path)
        limit = max(1, min(limit, 200))

        if _rg_available():
            args = ["--line-number", "--no-heading", "--color", "never",
                    "--max-count", "20"]
            if not case_sensitive:
                args.append("--ignore-case")
            if glob:
                args += ["--glob", glob]
            for ex in _EXCLUDES:
                args += ["--glob", ex]
            args += [pattern, str(base)]
            code, stdout = _run_rg(args, ctx.root, timeout=60)
        else:
            args = ["-n", "-I", "-E"]
            if not case_sensitive:
                args.append("-i")
            args += [pattern]
            if glob:
                args += ["--", glob]
            code, stdout = _run_git_grep(args, ctx.root, timeout=60)

        # rg/git grep both return 1 for "no matches", which is not an error.
        matches = _parse_matches(stdout, limit)
        for m in matches:
            p = Path(str(m["file"]))
            if p.is_absolute():
                m["file"] = relpath(ctx.root, p)

        return ToolResult(
            ok=True,
            summary=f"{len(matches)} match(es) for {pattern!r}"
                    + (f" in {glob}" if glob else ""),
            data={"matches": matches, "truncated": len(matches) >= limit},
        )

    @reg.add(
        "find_definition",
        "Find likely definition sites for a C++ symbol (class, struct, function, "
        "macro, alias or namespace). Heuristic, not a compiler front end: verify "
        "with read_file.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def find_definition(symbol: str, limit: int = 20) -> ToolResult:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
            raise ToolError("symbol must be a plain C++ identifier")

        pattern = "|".join(t.format(sym=re.escape(symbol)) for t in _DEF_TEMPLATES)
        result = search_text(pattern=pattern, limit=limit)
        result.summary = (
            f"{len(result.data['matches'])} candidate definition site(s) for "
            f"{symbol!r}"
        )
        return result
