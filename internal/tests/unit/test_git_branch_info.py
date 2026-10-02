"""git_branch_info states what 'my branch' is: detached or not, upstream or not, what changed."""
from __future__ import annotations

import subprocess

from local_agent.config import load_repo_config
from local_agent.tools import build_registry


def _git(root, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _branch_info(sandbox, base: str = "main"):
    registry, _ctx, _store = build_registry(load_repo_config(sandbox.root))
    return registry.get("git_branch_info").handler(base=base)


def _feature_branch(sandbox) -> str:
    _git(sandbox.root, "branch", "-M", "main")
    base = _git(sandbox.root, "rev-parse", "HEAD")
    _git(sandbox.root, "switch", "-q", "-c", "feature")
    text_util = sandbox.root / "src" / "text_util.cpp"
    text_util.write_text(text_util.read_text(encoding="utf-8") + "// feature\n", encoding="utf-8")
    (sandbox.root / "docs").mkdir(exist_ok=True)
    (sandbox.root / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(sandbox.root, "mv", "README.md", "README.txt")
    _git(sandbox.root, "add", "-A")
    _git(sandbox.root, "commit", "-qm", "feature work")
    return base


def test_a_branch_without_upstream_says_so_and_lists_what_changed(sandbox):
    base = _feature_branch(sandbox)
    data = _branch_info(sandbox).data
    assert (data["head"], data["detached"], data["upstream"]) == ("feature", False, None)
    assert "no upstream configured" in data["note"]
    assert data["merge_base"] == base
    assert {"status": "M", "path": "src/text_util.cpp"} in data["files_changed"]
    assert {"status": "A", "path": "docs/notes.md"} in data["files_changed"]
    assert {"status": "R", "path": "README.txt", "original_path": "README.md"} in data["files_changed"]


def test_a_configured_upstream_is_reported(sandbox):
    _feature_branch(sandbox)
    _git(sandbox.root, "branch", "--set-upstream-to=main", "feature")
    data = _branch_info(sandbox).data
    assert data["upstream"] == "main"
    assert "note" not in data


def test_a_detached_head_is_named_as_detached_not_as_a_branch(sandbox):
    base = _feature_branch(sandbox)
    commit = _git(sandbox.root, "rev-parse", "HEAD")
    _git(sandbox.root, "switch", "-q", "--detach", "HEAD")
    result = _branch_info(sandbox)
    data = result.data
    assert (data["head"], data["detached"], data["head_commit"]) == (None, True, commit)
    assert data["upstream"] is None and "detached" in data["note"]
    assert data["merge_base"] == base and data["files_changed"]
    assert result.summary == f"detached at {commit[:12]}"


def test_an_unresolvable_base_reports_no_merge_base_and_no_file_list(sandbox):
    data = _branch_info(sandbox, base="origin/does-not-exist").data
    assert data["merge_base"] is None and data["files_changed"] is None
    assert "not resolvable" in data["note"]
