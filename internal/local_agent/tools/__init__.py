"""Tool layer assembly."""

from __future__ import annotations

from ..config import RepoConfig
from . import build as build_tools
from . import files as file_tools
from . import git as git_tools
from . import patch as patch_tools
from . import search as search_tools
from . import testing as test_tools
from .base import Risk, Tool, ToolError, ToolRegistry, ToolResult, SandboxError
from .context import ToolContext
from .patch import PatchStore

__all__ = [
    "Risk",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "SandboxError",
    "ToolContext",
    "PatchStore",
    "build_registry",
    "TOOLSETS",
]


# Progressive disclosure of capability. A skill sees only the tools its
# procedure needs. Fewer tools, fewer ways to go sideways.
TOOLSETS: dict[str, list[str]] = {
    "repo-navigation": [
        "repo_info", "list_files", "read_file", "search_text", "find_definition",
    ],
    "git-review": [
        "git_status", "git_diff", "git_log", "git_show", "git_branch_info",
        "read_file", "search_text",
    ],
    "prepare-commit": [
        "git_status", "git_diff", "git_branch_info", "read_file",
        "git_stage", "git_commit",
    ],
    "build-and-test": [
        "repo_info", "configure_project", "build_target", "list_tests", "run_test",
        "read_file", "read_log_chunk",
    ],
    "diagnose-build-failure": [
        "build_target", "read_log_chunk", "read_file", "search_text",
        "find_definition", "propose_patch", "apply_patch",
    ],
    "diagnose-test-failure": [
        "run_test", "read_log_chunk", "read_file", "search_text",
        "find_definition", "git_diff", "propose_patch", "apply_patch",
    ],
    # Fallback when no skill has been activated yet.
    "_default": [
        "repo_info", "list_files", "read_file", "search_text", "git_status",
    ],
}


# Available to every skill, always. A run must be able to state its conclusion
# in a checkable form no matter which procedure it is following.
UNIVERSAL_TOOLS = ["submit_answer"]


def build_registry(
    repo: RepoConfig, journal: object | None = None
) -> tuple[ToolRegistry, ToolContext, PatchStore]:
    ctx = ToolContext(repo=repo)
    reg = ToolRegistry()
    store = PatchStore(journal=journal)

    file_tools.register(reg, ctx)
    search_tools.register(reg, ctx)
    git_tools.register(reg, ctx, journal)
    build_tools.register(reg, ctx)
    test_tools.register(reg, ctx)
    patch_tools.register(reg, ctx, store)

    return reg, ctx, store
