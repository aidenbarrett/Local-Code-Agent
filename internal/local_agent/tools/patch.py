"""Proposing and applying edits.

Two separate tools on purpose. `propose_patch` is free: it computes a unified
diff and shows it, touching nothing. `apply_patch` can only apply a patch that
was already proposed and shown, and is gated by policy. The model cannot write
a byte to the worktree that a human has not already seen as a diff.
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tool_primitives import (
    BlockedError,
    Risk,
    ToolError,
    ToolRegistry,
    ToolResult,
    assert_writable,
    relpath,
    resolve_in_repo,
)
from .tool_context import ToolContext


def new_patch_id() -> str:
    """A patch id no tool-call parser can read as a number.

    A bare hex id such as ``43477e22`` is a valid float literal. A server-side tool
    parser that coerces parameter values turned it into ``4.3477e22`` and the model
    could never apply its own patch. The leading letter keeps every id a string.
    """
    return "p" + uuid.uuid4().hex[:8]


def _protected(ctx: ToolContext) -> tuple[str, ...]:
    """Paths inside the repository that a patch may never touch.

    The build directory holds the binaries AND the record of when they were
    built. The run directory holds this run's own journal and logs. `.git`
    holds the history the evaluator reads. None of the three is the project.
    """
    return (ctx.repo.build_dir, ctx.repo.run_dir, ".local-agent", ".git")


@dataclass
class PendingPatch:
    patch_id: str
    path: Path
    original: str | None  # None: a new file, which must still not exist at apply time
    updated: str
    diff: str


class PatchStore:
    """Pending proposals, plus a journal of what was actually written.

    The journal is what lets an escalation hand the strong tier a clean tree
    instead of one the cheap tier has already broken.
    """

    def __init__(self, journal: Any | None = None) -> None:
        self._pending: dict[str, PendingPatch] = {}
        # Replaced id -> (replacement id, whether the replacement includes its edit).
        self._superseded: dict[str, tuple[str, bool]] = {}
        self.journal = journal

    def pending_for(self, path: Path) -> PendingPatch | None:
        """The one pending proposal for this file, if any."""
        return next((item for item in self._pending.values() if item.path == path), None)

    def put(self, patch: PendingPatch, *, includes_previous: bool = False) -> str | None:
        """Keep one pending proposal per file and remember what replaced the old one.

        ``includes_previous`` says the new proposal was built on the pending one's
        result, so its edit is carried forward rather than dropped.
        """
        if patch.patch_id in self._pending or patch.patch_id in self._superseded:
            raise ToolError(f"patch id {patch.patch_id!r} is already used")
        previous = self.pending_for(patch.path)
        if previous is not None:
            del self._pending[previous.patch_id]
            self._superseded[previous.patch_id] = (patch.patch_id, includes_previous)
        self._pending[patch.patch_id] = patch
        return previous.patch_id if previous is not None else None

    def take(self, patch_id: str) -> PendingPatch:
        if patch_id in self._superseded:
            replacement, included = self._superseded[patch_id]
            while replacement in self._superseded:
                replacement, carried = self._superseded[replacement]
                included = included and carried
            if replacement not in self._pending:
                raise ToolError(
                    f"{patch_id} was superseded by {replacement}, which is no longer pending. "
                    "Read the current file and propose again if another edit is needed."
                )
            if included:
                raise ToolError(
                    f"{patch_id} is included in {replacement}; apply {replacement}, "
                    "which carries both edits."
                )
            raise ToolError(
                f"{patch_id} was replaced by {replacement}, which does not contain its edit. "
                f"Apply {replacement}, then propose {patch_id}'s edit again if it is still needed."
            )
        if patch_id not in self._pending:
            raise ToolError(
                f"unknown patch id {patch_id!r}. Propose the patch first; the agent "
                "does not apply edits it has not shown."
            )
        return self._pending.pop(patch_id)

    def __len__(self) -> int:
        return len(self._pending)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


_READ_FILE_NUMBER = re.compile(r"^\d+: ?")


def _without_read_file_numbers(text: str) -> str | None:
    """`text` with read_file's `N: ` line prefixes removed, if every nonblank line has one.

    read_file shows lines as `13:     ++count;`. Small models copy that display back
    into `find`, which then matches nothing. Only the display format read_file itself
    produces is recognised, and only when every nonblank line carries it.
    """
    lines = text.split("\n")
    nonblank = [line for line in lines if line.strip()]
    if not nonblank or not all(_READ_FILE_NUMBER.match(line) for line in nonblank):
        return None
    return "\n".join(_READ_FILE_NUMBER.sub("", line, count=1) if line.strip() else line
                     for line in lines)


def _tolerant_replace(original: str, find: str, replace: str) -> tuple[str | None, int]:
    """Replace the one block of whole lines matching `find` up to per-line whitespace.

    Used only after an exact match failed. Lines are compared with leading and
    trailing whitespace stripped; blank lines at either end of `find` are ignored.
    Returns (updated text or None, number of matching blocks). The replacement is
    re-indented by the difference between the model's indentation and the file's.
    """
    wanted = find.splitlines()
    while wanted and not wanted[0].strip():
        wanted.pop(0)
    while wanted and not wanted[-1].strip():
        wanted.pop()
    if not wanted:
        return None, 0
    key = [line.strip() for line in wanted]
    lines = original.splitlines(keepends=True)
    starts = [
        i for i in range(len(lines) - len(key) + 1)
        if [lines[i + k].strip() for k in range(len(key))] == key
    ]
    if len(starts) != 1:
        return None, len(starts)
    start = starts[0]
    model_indent = _indent(wanted[0])
    file_indent = _indent(lines[start])
    # Nesting inside the block: the file may use a different indent width than the
    # model (4 vs 2 spaces). Learn one consistent ratio from the matched lines; with
    # tabs or an inconsistent ratio, only the base indent is shifted.
    ratio = 1.0
    spaces_only = "\t" not in model_indent + file_indent + "".join(
        _indent(line) for line in wanted + lines[start:start + len(key)]
    )
    if spaces_only:
        seen = {
            (len(_indent(lines[start + k])) - len(file_indent))
            / (len(_indent(wanted[k])) - len(model_indent))
            for k in range(len(key))
            if wanted[k].strip() and len(_indent(wanted[k])) > len(model_indent)
        }
        if len(seen) == 1 and next(iter(seen)) > 0:
            ratio = next(iter(seen))
    file_block = [_indent(line) for line in lines[start:start + len(key)]]
    file_uses_tabs = (
        all(not i.strip("\t") for i in file_block) and any("\t" in i for i in file_block)
    )
    model_depths = [
        len(_indent(w)) - len(model_indent) for w in wanted
        if w.strip() and not _indent(w).strip(" ") and len(_indent(w)) > len(model_indent)
    ]
    tab_unit = min(model_depths) if file_uses_tabs and model_depths else None

    def reindent(line: str) -> str:
        """One replacement line moved from the model's indentation onto the file's."""
        if not line.startswith(model_indent):
            return line
        extra = _indent(line)[len(model_indent):]
        if tab_unit and line.strip() and not extra.strip(" "):
            return file_indent + "\t" * round(len(extra) / tab_unit) + line.lstrip(" \t")
        if not line.strip():
            return line
        if spaces_only and not extra.strip(" "):
            return file_indent + " " * round(len(extra) * ratio) + line.lstrip(" ")
        return file_indent + line[len(model_indent):]

    body = [reindent(line) for line in replace.splitlines()]
    last = lines[start + len(key) - 1]
    ending = "\n" if last.endswith("\n") else ""
    replacement = "\n".join(body) + (ending if body else "")
    return "".join(lines[:start]) + replacement + "".join(lines[start + len(key):]), 1


def _replace_once(text: str, find: str, replace: str) -> tuple[str, str]:
    """Apply one find/replace to ``text``: (updated text, match kind), or ToolError."""
    numbered: str | None = None
    if text.count(find) == 0 and _without_read_file_numbers(text) is None:
        numbered = _without_read_file_numbers(find)
    if numbered is not None:
        # The model echoed read_file's line numbers. Match the lines it meant; a
        # replacement written in the same display format loses its numbers too.
        find = numbered
        replace = _without_read_file_numbers(replace) or replace
    occurrences = text.count(find)
    match = "exact"
    if occurrences > 1:
        raise ToolError(
            f"`find` text appears {occurrences} times. Widen it with surrounding "
            "lines until it is unique."
        )
    if occurrences == 1:
        updated = text.replace(find, replace, 1)
    else:
        tolerant, blocks = _tolerant_replace(text, find, replace)
        if blocks > 1:
            raise ToolError(
                f"`find` matches {blocks} places once whitespace is ignored. Widen it "
                "with surrounding lines until it is unique."
            )
        if tolerant is None:
            raise ToolError(
                "`find` text does not appear in the file, even ignoring indentation "
                "and trailing spaces. Read the exact lines first."
            )
        updated = tolerant
        match = "whitespace_tolerant"
    if numbered is not None:
        match = ("line_numbers_stripped" if match == "exact"
                 else "line_numbers_stripped_whitespace_tolerant")
    return updated, match


def _changed_lines(before: str, after: str) -> list[tuple[int, int]]:
    """Line ranges of ``before`` that ``after`` rewrites, deletes or inserts at."""
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(keepends=True), after.splitlines(keepends=True),
        autojunk=False)
    return [(i1, i2) for tag, i1, i2, _j1, _j2 in matcher.get_opcodes() if tag != "equal"]


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    (a1, a2), (b1, b2) = a, b
    if a1 == a2 and b1 == b2:
        return a1 == b1  # two insertions at the same point
    if a1 == a2:
        return b1 < a1 < b2  # an insertion inside a rewritten range
    if b1 == b2:
        return a1 < b1 < a2
    return max(a1, b1) < min(a2, b2)


def _changes_overlap(original: str, first: str, second: str) -> bool:
    """Do two edits of ``original`` rewrite any of the same lines?"""
    return any(_ranges_overlap(a, b)
               for a in _changed_lines(original, first)
               for b in _changed_lines(original, second))


def register(reg: ToolRegistry, ctx: ToolContext, store: PatchStore) -> None:
    @reg.add(
        "propose_patch",
        "Propose an exact text replacement in one file and return the unified diff. "
        "Changes nothing on disk. `find` must appear exactly once in the file. "
        "If this file already has a pending proposal, an edit to other lines is added to "
        "it and an edit to the same lines replaces it; either way apply only the newest id.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {
                    "type": "string",
                    "description": "Exact existing text, including indentation. Must be unique.",
                },
                "replace": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["path", "find", "replace"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def propose_patch(
        path: str, find: str, replace: str, rationale: str = ""
    ) -> ToolResult:
        target = resolve_in_repo(ctx.root, path)
        # Inside the repository is not the same as fair game. Refused here, at
        # the free step, so the attempt is recorded before anything is written.
        assert_writable(ctx.root, target, _protected(ctx))
        if not target.is_file():
            raise ToolError(f"{path!r} is not a file")

        original = target.read_text(encoding="utf-8")
        if not find.strip():
            # An empty find matches everywhere, and "appears N times" would send a
            # small model hunting for a problem it does not have.
            raise ToolError(
                "`find` is empty. To add lines at the start of a file, use its first "
                "line as `find` and put the new lines before it in `replace`."
            )
        # One pending proposal per file. A new edit to other lines builds on it, so
        # both edits land with one apply; a new edit to the lines it already
        # changes is a correction that replaces it, and says so.
        pending = store.pending_for(target)
        if pending is not None and pending.original != original:
            pending = None  # stale: the file changed under it, so it can never apply
        stacked_on: str | None = None
        try:
            updated, match = _replace_once(original, find, replace)
        except ToolError:
            if pending is None:
                raise
            # Not in the file on disk: the model is editing text it proposed.
            updated, match = _replace_once(pending.updated, find, replace)
            stacked_on = pending.patch_id
        else:
            if pending is not None and not _changes_overlap(
                    original, updated, pending.updated):
                try:
                    updated, match = _replace_once(pending.updated, find, replace)
                    stacked_on = pending.patch_id
                except ToolError:
                    stacked_on = None  # ambiguous on the combined text: replace instead
        rel = relpath(ctx.root, target)
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        patch_id = new_patch_id()
        superseded = store.put(PendingPatch(patch_id, target, original, updated, diff),
                               includes_previous=stacked_on is not None)
        note = {
            "exact": "",
            "whitespace_tolerant": "; matched ignoring indentation/trailing spaces, check the diff",
        }.get(match, "; matched after removing read_file line numbers from find, check the diff")
        if stacked_on is not None:
            replacement_note = f"; includes {stacked_on}, apply {patch_id} only"
        elif superseded:
            replacement_note = f"; replaces {superseded}, whose edit is dropped"
        else:
            replacement_note = ""
        return ToolResult(
            ok=True,
            summary=f"patch {patch_id} proposed for {rel} (nothing written yet{note})"
                    + replacement_note,
            data={
                "patch_id": patch_id,
                "path": rel,
                "rationale": rationale,
                "diff": diff,
                "match": match,
                "supersedes": superseded,
                "includes": stacked_on,
            },
        )

    @reg.add(
        "propose_file",
        "Propose creating one NEW file with the given content and return the diff. "
        "Changes nothing on disk. Refuses if the file already exists: use "
        "propose_patch to change an existing file. Apply it with apply_patch. "
        "Replaces any pending proposal for this file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        Risk.READ,
    )
    def propose_file(path: str, content: str, rationale: str = "") -> ToolResult:
        target = resolve_in_repo(ctx.root, path)
        assert_writable(ctx.root, target, _protected(ctx))
        if target.exists() or target.is_symlink():
            raise ToolError(f"{path!r} already exists. Use propose_patch to change it.")
        if not content:
            raise ToolError("a new file needs content")
        rel = relpath(ctx.root, target)
        diff = "".join(
            difflib.unified_diff(
                [], content.splitlines(keepends=True),
                fromfile="/dev/null", tofile=f"b/{rel}", n=3,
            )
        )
        patch_id = new_patch_id()
        superseded = store.put(PendingPatch(patch_id, target, None, content, diff))
        replacement_note = (f"; replaces {superseded}, whose content is dropped"
                            if superseded else "")
        return ToolResult(
            ok=True,
            summary=f"new file {patch_id} proposed for {rel} (nothing written yet)"
                    + replacement_note,
            data={"patch_id": patch_id, "path": rel, "rationale": rationale, "diff": diff,
                  "supersedes": superseded},
        )

    @reg.add(
        "apply_patch",
        "Apply a patch that was previously returned by propose_patch. Requires "
        "approval. Refuses if the file changed since the patch was proposed.",
        {
            "type": "object",
            "properties": {"patch_id": {"type": "string"}},
            "required": ["patch_id"],
            "additionalProperties": False,
        },
        Risk.DANGEROUS,
    )
    def apply_patch(patch_id: str) -> ToolResult:
        if not ctx.repo.policy.allow_patch:
            raise BlockedError(
                "applying patches is disabled by repository policy "
                "(policy.allow_patch = false)"
            )
        patch = store.take(patch_id)
        # Checked again at the write. propose_patch already refused this, so
        # reaching here means the guard list changed underneath a stored patch,
        # and the write is the moment that matters.
        assert_writable(ctx.root, patch.path, _protected(ctx))
        if patch.original is None:
            if patch.path.exists() or patch.path.is_symlink():
                raise ToolError(
                    f"{relpath(ctx.root, patch.path)} was created since new file "
                    f"{patch_id} was proposed. Read it and propose a change instead."
                )
            patch.path.parent.mkdir(parents=True, exist_ok=True)
            # Refuse, rather than follow, a parent that resolved outside the repo.
            assert_writable(ctx.root, patch.path, _protected(ctx))
        else:
            current = patch.path.read_text(encoding="utf-8")
            if current != patch.original:
                raise ToolError(
                    f"{relpath(ctx.root, patch.path)} changed since patch {patch_id} was "
                    "proposed. Re-read the file and propose again."
                )
        patch.path.write_text(patch.updated, encoding="utf-8")
        if store.journal is not None:
            store.journal.record_write(
                patch.path, patch.original, patch.updated, "apply_patch"
            )
        return ToolResult(
            ok=True,
            summary=f"applied patch {patch_id} to {relpath(ctx.root, patch.path)}",
            data={"path": relpath(ctx.root, patch.path), "diff": patch.diff},
        )
