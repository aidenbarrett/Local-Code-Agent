"""Repository state capture, mutation journalling, and evidence packets.

Two problems, both about what the strong tier inherits when a cheap attempt is
escalated.

**The filesystem.** "Fresh context" is not enough. If the cheap tier applied a
bad patch before it failed, the strong tier opens a contaminated worktree and
diagnoses a defect that the agent itself introduced. That is worse than
contaminated prose, because it looks like real evidence.

So every run fingerprints the repository before it starts, journals every
mutation it makes itself, and reverts only its own mutations before escalating.
It never touches anything it did not write. No `reset --hard`, no `checkout .`,
no `clean`. If the fingerprint does not come back to its starting value, the run
says so loudly rather than pretending.

**The evidence.** Throwing away the cheap attempt entirely also throws away
expensive deterministic observations: real compiler diagnostics, real ctest
output, real exit codes. Those are ground truth and re-running them costs
seconds of build time. So the strong tier gets them back as an evidence packet,
with the cheap model's reasoning stripped out. Commands, exit codes, structured
diagnostics and artifact paths. No "I think the problem is X".
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_GIT_TIMEOUT = 60


def _git(root: Path, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(frozen=True)
class RepoFingerprint:
    """Enough to tell whether the tree we hand over is the tree we started with."""

    head: str | None
    dirty_diff_sha: str
    untracked_sha: str
    untracked_count: int
    config_sha: str
    skill_sha: str | None = None

    @staticmethod
    def capture(root: Path, skill_body: str | None = None) -> "RepoFingerprint":
        _, head = _git(root, ["rev-parse", "HEAD"])
        _, diff = _git(root, ["diff", "--no-ext-diff", "HEAD"])
        _, untracked = _git(root, ["ls-files", "--others", "--exclude-standard"])
        config = root / ".local-agent.toml"
        return RepoFingerprint(
            head=head.strip() or None,
            dirty_diff_sha=_sha(diff),
            untracked_sha=_sha(untracked),
            untracked_count=len([ln for ln in untracked.splitlines() if ln.strip()]),
            config_sha=_sha(config.read_text(encoding="utf-8")) if config.is_file() else "",
            skill_sha=_sha(skill_body) if skill_body else None,
        )

    def differs_from(self, other: "RepoFingerprint") -> list[str]:
        diffs = []
        if self.head != other.head:
            diffs.append(f"HEAD moved ({other.head} -> {self.head})")
        if self.dirty_diff_sha != other.dirty_diff_sha:
            diffs.append("tracked files differ")
        if self.untracked_sha != other.untracked_sha:
            diffs.append(
                f"untracked files differ ({other.untracked_count} -> {self.untracked_count})"
            )
        if self.config_sha != other.config_sha:
            diffs.append(".local-agent.toml changed")
        return diffs

    def as_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "dirty_diff_sha": self.dirty_diff_sha,
            "untracked_sha": self.untracked_sha,
            "untracked_count": self.untracked_count,
            "config_sha": self.config_sha,
            "skill_sha": self.skill_sha,
        }


@dataclass
class FileMutation:
    path: Path
    before: str
    after: str
    tool: str


@dataclass
class MutationJournal:
    """Every file this agent wrote, and what it looked like beforehand."""

    entries: list[FileMutation] = field(default_factory=list)
    irreversible: list[str] = field(default_factory=list)

    def record_write(self, path: Path, before: str, after: str, tool: str) -> None:
        self.entries.append(FileMutation(path, before, after, tool))

    def record_irreversible(self, description: str) -> None:
        """Something we cannot safely undo, such as a commit."""
        self.irreversible.append(description)

    @property
    def touched(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            name = str(entry.path)
            if name not in seen:
                seen.append(name)
        return seen

    def revert(self) -> dict[str, Any]:
        """Undo our own writes, newest first, and never anybody else's.

        A file whose current content is not what we last wrote has been changed
        by someone else since. We leave it alone and report it, because
        clobbering a person's edit to tidy up after ourselves would be
        unforgivable.
        """
        reverted: list[str] = []
        skipped: list[str] = []
        for entry in reversed(self.entries):
            try:
                current = entry.path.read_text(encoding="utf-8")
            except OSError:
                skipped.append(f"{entry.path} (unreadable)")
                continue
            if current != entry.after:
                skipped.append(f"{entry.path} (changed by someone else since)")
                continue
            entry.path.write_text(entry.before, encoding="utf-8")
            reverted.append(str(entry.path))

        result = {
            "reverted": reverted,
            "skipped": skipped,
            "irreversible": list(self.irreversible),
        }
        if not skipped:
            self.entries.clear()
        return result


# --------------------------------------------------------------------------
# evidence packet
# --------------------------------------------------------------------------

# Fields worth carrying forward from a tool result. Everything else, including
# anything the cheap model said about them, is dropped.
_EVIDENCE_KEYS = (
    "command", "profile", "elapsed_s", "error_count", "warning_count",
    "errors", "link_errors", "cmake_errors", "failed", "totals",
    "assertions", "crash_markers", "ctest_reported_timeouts",
    "killed_by_orchestrator", "tests", "files", "matches",
)


def build_evidence_packet(
    history: list[Any],
    fingerprint: RepoFingerprint,
    max_items: int = 12,
) -> dict[str, Any]:
    """Ground truth only: what was run, what it returned, where the log is.

    Deliberately contains no model output. The strong tier is told what was
    observed, not what the cheap tier concluded from it, so it cannot be
    anchored on a wrong diagnosis.
    """
    observations = []
    for index, record in enumerate(history):
        if not getattr(record, "execution", "ok") == "ok":
            continue  # only things that actually ran are evidence
        data = getattr(record, "evidence", None) or {}
        kept = {k: v for k, v in data.items() if k in _EVIDENCE_KEYS and v not in (None, [], {})}
        observations.append(
            {
                "id": f"{record.name}:{index}",
                "tool": record.name,
                "arguments": record.arguments,
                "result": record.domain,
                "exit_code": getattr(record, "exit_code", None),
                "summary": record.summary,
                "artifacts": getattr(record, "artifacts", []),
                **({"data": kept} if kept else {}),
            }
        )
        if len(observations) >= max_items:
            break

    return {
        "repo_state_at_start": fingerprint.as_dict(),
        "observations": observations,
        "note": (
            "These are deterministic observations from a previous attempt at "
            "this same task, replayed so you do not have to pay for them again. "
            "They contain no reasoning or conclusions from that attempt. The "
            "repository has been restored to the state recorded above. Re-run "
            "any tool whose result you do not trust."
        ),
    }


def render_evidence_message(packet: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "system",
        "content": "Evidence from a previous attempt:\n" + json.dumps(packet, indent=2, default=str),
    }
