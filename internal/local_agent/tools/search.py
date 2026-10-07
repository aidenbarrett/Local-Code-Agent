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

from ..cpp_symbols import parse_cpp_symbol
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
_FUNCTION_SITE = (
    # Keep this portable between ripgrep's default engine and ``git grep -E``:
    # neither the fallback contract nor C++ lookup may depend on lookbehind.
    r"(^|[^A-Za-z0-9_]){sym}\s*\([^;{{}}]*\)\s*"
    r"(\[\[[^\]]+\]\]\s*)*"
    r"(const\b\s*)?(volatile\b\s*)?(&{{1,2}}\s*)?"
    r"(noexcept(\s*\([^)]*\))?\s*)?"
    r"((override|final)\b\s*)*"
    r"(->\s*[^;{{=]+)?\s*"
    r"(;|\{{|=\s*(0|default|delete)\s*;)"
)
_QUALIFIED_DEF_TEMPLATES = (
    _FUNCTION_SITE,
    r"\b{sym}\s*=",
)
_DEF_TEMPLATES = (
    r"(class|struct|enum class|enum|union)\s+{sym}\b",
    *_QUALIFIED_DEF_TEMPLATES,
    r"#define\s+{sym}\b",
    r"using\s+{sym}\s*=",
    r"namespace\s+{sym}\b",
)

def _definition_pattern(symbol: str) -> str:
    """Build a bounded declaration/definition query without discarding qualification."""
    parsed = parse_cpp_symbol(symbol)
    parts = parsed.components
    forms: tuple[str, ...]
    templates: tuple[str, ...]
    if len(parts) == 1:
        forms = (parsed.text,)
        templates = _DEF_TEMPLATES
    else:
        # A definition inside ``namespace sandbox`` is commonly spelt
        # ``RingBuffer::full`` rather than ``sandbox::RingBuffer::full``. Keep
        # the member qualification, but never fall back to bare ``full`` where
        # unrelated classes would become indistinguishable.
        forms = ((parsed.text,) if len(parts) == 2
                 else (parsed.text, "::".join(parts[-2:])))
        templates = _QUALIFIED_DEF_TEMPLATES
    return "|".join(
        template.format(sym=re.escape(form))
        for form in dict.fromkeys(forms)
        for template in templates
    )


# Words that, directly before a name and its argument list, make the line a
# statement using the name rather than a declaration of it.
_STATEMENT_WORDS = frozenset({
    "return", "co_return", "co_yield", "co_await", "throw", "else", "do", "case",
    "goto", "new", "delete", "sizeof", "alignof", "typeid", "decltype", "and", "or",
    "not",
})
# A prefix ending in one of these is an expression, never a declarator.
_EXPRESSION_ENDINGS = ("=", "(", ",", "!", "?", "+", "-", "/", "%", "|", "^", "[", "{",
                       "}", ";", ".")


def _declaration_shaped(text: str, form: str) -> bool:
    """Whether a candidate line declares or defines ``form`` rather than calls it.

    The search regex must stay portable to ``git grep -E`` (no lookaround), so call
    shapes are filtered here: ``frobnicate(3);``, ``return Widget::ready();``,
    ``total += util::parse(a);`` and ``if (ready(x)) {`` are uses, not sites.
    Type, macro, alias and namespace forms never reach this filter.
    """
    position = re.search(rf"(?<![A-Za-z0-9_]){re.escape(form)}\s*\(", text)
    if position is None:
        return True  # matched by a non-function form (e.g. a static initialiser)
    prefix = text[:position.start()].rstrip()
    while prefix.endswith("::"):
        # ``void ns::frobnicate()``: judge what precedes the qualifier chain.
        prefix = re.sub(r"(?:[A-Za-z_][A-Za-z0-9_]*)?::$", "", prefix).rstrip()
    rest = text[position.end():]
    if not prefix:
        # A bare ``name(...)`` line is a call statement unless it opens a body or is
        # defaulted/deleted (an out-of-line constructor or destructor).
        return bool(re.search(r"\)[^;]*(\{|=\s*(default|delete)\s*;)", rest))
    if prefix.endswith(_EXPRESSION_ENDINGS) or prefix.endswith(("&&", "||", "->", ":")):
        return False  # ``p->f()``, ``a && f()``, ``case 1: f();``: uses
    if prefix.endswith(")") and re.search(r"\b(if|while|for|switch)\s*\(", prefix):
        return False  # ``if (ok) f(x);``: a statement under a condition
    last = re.split(r"[\s*&]+", prefix)[-1] or prefix.split()[-1]
    return last not in _STATEMENT_WORDS


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


def _git_grep_pathspecs(root: Path, base: Path, glob: str | None) -> list[str]:
    """Translate the rg search scope to git pathspecs without widening it."""
    # Pathspecs always use "/"; relpath() uses the OS separator.
    base_path = Path(relpath(root, base)).as_posix()
    include = (
        (glob or ".")
        if base_path == "."
        else f"{base_path}/{glob}" if glob else base_path
    )
    pathspecs = [include]
    for excluded in _EXCLUDES:
        pattern = excluded.removeprefix("!").removesuffix("/")
        # ripgrep's `!build/` excludes a directory of that name at any depth.
        pathspecs.append(f":(top,exclude,glob)**/{pattern}/**")
    return pathspecs


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
            # --untracked: a file the user (or the agent) has just created is part of
            # the working tree. Without it the fallback silently searched only what
            # git already tracks, while ripgrep searched everything.
            args = ["-n", "-I", "-E", "--untracked"]
            if not case_sensitive:
                args.append("-i")
            args += [pattern, "--", *_git_grep_pathspecs(ctx.root, base, glob)]
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
        "Find likely declaration or definition sites for a supported C++ symbol "
        "(class, struct, function, macro, alias or namespace). This bounded textual "
        "search does not accept template-ids. A zero result means not found by this "
        "heuristic, not that the symbol is absent; verify candidates with read_file.",
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
        try:
            pattern = _definition_pattern(symbol)
        except ValueError as exc:
            reason = str(exc)
            if reason == "cpp_template_symbol_unsupported":
                raise ToolError("template-id symbol lookup is not supported") from exc
            raise ToolError(
                "symbol must be a C++ identifier, optionally qualified with ::; "
                "a destructor may be the final component"
            ) from exc
        # Search wider than asked, drop call shapes, then cut to the caller's limit:
        # uses must not crowd real sites out of the result.
        result = search_text(pattern=pattern, limit=200)
        parsed = parse_cpp_symbol(symbol)
        forms = {parsed.text, "::".join(parsed.components[-2:])}
        kept = [
            match for match in result.data["matches"]
            if any(_declaration_shaped(str(match["text"]), form) for form in forms
                   if form in str(match["text"]))
        ]
        cap = max(1, min(limit, 200))
        result.data["matches"] = kept[:cap]
        result.data["truncated"] = bool(result.data["truncated"]) or len(kept) > cap
        result.summary = (
            f"{len(result.data['matches'])} candidate declaration/definition site(s) for "
            f"{parsed.text!r}"
        )
        return result
