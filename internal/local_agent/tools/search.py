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

from .tool_primitives import Risk, ToolError, ToolRegistry, ToolResult, relpath, resolve_in_repo
from .tool_context import ToolContext

_EXCLUDES = [
    "!build/", "!out/", "!.git/", "!node_modules/", "!.local-agent/",
    "!cmake-build-*/",
]

# Rough C++ definition shapes. Deliberately crude; it feeds `read_file`, it does
# not pretend to be a compiler front end.
# A qualified name (``RingBuffer::full``) is defined out of line as a function body
# or a static member initialiser; the type, macro and namespace shapes do not apply.
_QUALIFIED_DEF_TEMPLATES = (
    r"\b{sym}\s*\([^;]*\)\s*(const)?\s*(noexcept)?\s*\{{",
    r"\b{sym}\s*=",
)
_DEF_TEMPLATES = (
    r"(class|struct|enum class|enum|union)\s+{sym}\b",
    *_QUALIFIED_DEF_TEMPLATES,
    r"#define\s+{sym}\b",
    r"using\s+{sym}\s*=",
    r"namespace\s+{sym}\b",
)

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_QUALIFIED_IDENTIFIER = re.compile(rf"{_IDENTIFIER}(?:::{_IDENTIFIER})*")


def _definition_pattern(symbol: str) -> str:
    """Build a bounded textual definition query without discarding qualification."""
    parts = symbol.split("::")
    forms: tuple[str, ...]
    templates: tuple[str, ...]
    if len(parts) == 1:
        forms = (symbol,)
        templates = _DEF_TEMPLATES
    else:
        # A definition inside ``namespace sandbox`` is commonly spelt
        # ``RingBuffer::full`` rather than ``sandbox::RingBuffer::full``. Keep
        # the member qualification, but never fall back to bare ``full`` where
        # unrelated classes would become indistinguishable.
        forms = (symbol,) if len(parts) == 2 else (symbol, "::".join(parts[-2:]))
        templates = _QUALIFIED_DEF_TEMPLATES
    return "|".join(
        template.format(sym=re.escape(form))
        for form in dict.fromkeys(forms)
        for template in templates
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
        "Search text CONTENTS in repository files for a regular expression. "
        "The pattern is matched against file contents, not filenames or paths. "
        "Use list_files to discover files by name/extension; use glob here only "
        "to restrict which filenames are searched. Returns file, line and the "
        "matching text.",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression matched against file contents, not filenames.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional filename glob restricting searched files, such as *.cpp.",
                },
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
        if not _QUALIFIED_IDENTIFIER.fullmatch(symbol):
            raise ToolError("symbol must be a C++ identifier, optionally qualified with ::")

        pattern = _definition_pattern(symbol)
        result = search_text(pattern=pattern, limit=limit)
        result.summary = (
            f"{len(result.data['matches'])} candidate definition site(s) for "
            f"{symbol!r}"
        )
        return result
