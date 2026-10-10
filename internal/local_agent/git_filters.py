"""Which configured Git filter drivers a repository's files actually use (#398, #464).

A clean, smudge or process filter is a program named in Git configuration. Git runs it
for ordinary reads too: ``git status`` and worktree ``git diff`` run the clean filter
(observed with git 2.43, even on an unchanged tree). So neither the controller's
candidate checks nor the read-only Git tools may run a command that touches the worktree
while a configured filter is in use; they refuse instead. Stripping the filter is no
answer, because it changes what the bytes mean.

This is the one detector both use. It needs only ``git config``, ``git ls-files`` and
``git check-attr``, none of which runs a filter. Anything it cannot establish is
reported as unknown, never as "no filter in use".
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

# Runs ``git <args>`` in the repository with optional stdin; returns (code, stdout, stderr).
GitRunner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, bytes]]


class GitFilterCheckError(Exception):
    """Whether the repository's files use a Git filter could not be established."""


def split_z(raw: bytes) -> tuple[str, ...]:
    """NUL-separated git output as text; undecodable bytes survive as surrogates."""
    return tuple(item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item)


def _read(run: GitRunner, args: Sequence[str], stdin: bytes | None = None) -> bytes:
    code, out, err = run(args, stdin)
    if code != 0:
        message = err.decode("utf-8", "replace").strip()
        raise GitFilterCheckError(f"git {args[0]} failed ({code}): {message}")
    return out


def filter_drivers_in_use(run: GitRunner) -> tuple[str, ...]:
    """Configured clean/smudge/process filter drivers that some file in the repository uses."""
    code, out, err = run(
        ["config", "--null", "--name-only", "--get-regexp",
         r"^filter\..*\.(clean|smudge|process)$"],
        None,
    )
    if code not in (0, 1):  # 1 is "no such key"; anything else is unknown
        message = err.decode("utf-8", "replace").strip()
        raise GitFilterCheckError(f"git config could not be read ({code}): {message}")
    drivers = set()
    for entry in split_z(out):
        name = entry.split(".", 1)[1].rsplit(".", 1)[0] if "." in entry else ""
        if name:
            drivers.add(name)
    if not drivers:
        return ()
    files = _read(run, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    requested = split_z(files)
    if not requested:
        return ()
    attrs = split_z(_read(run, ["check-attr", "-z", "--stdin", "filter"], files))
    answered_paths = attrs[0::3]
    answered_attributes = attrs[1::3]
    if (len(attrs) != len(requested) * 3
            or sorted(answered_paths) != sorted(requested)
            or any(attribute != "filter" for attribute in answered_attributes)):
        raise GitFilterCheckError(
            "git check-attr did not return one complete answer for every requested path"
        )
    used = {attrs[i + 2] for i in range(0, len(attrs), 3)}
    return tuple(sorted(used & drivers))


__all__ = ["GitFilterCheckError", "GitRunner", "filter_drivers_in_use", "split_z"]
