from __future__ import annotations

from pathlib import Path

from local_agent.agent.policy import PolicyEngine, Verdict
from local_agent.agent.skills import SkillLibrary, _parse_frontmatter
from local_agent.config import Policy
from local_agent.tools.base import Risk, Tool

REPO = Path(__file__).resolve().parent.parent.parent


def _tool(name: str, risk: Risk) -> Tool:
    return Tool(name, "", {}, lambda: None, risk)


def test_read_tools_never_need_approval():
    engine = PolicyEngine(Policy())
    assert engine.check(_tool("read_file", Risk.READ), {}).verdict is Verdict.ALLOW


def test_build_denied_when_disabled():
    engine = PolicyEngine(Policy(allow_build=False))
    decision = engine.check(_tool("build_target", Risk.EXECUTE), {})
    assert decision.verdict is Verdict.DENY
    assert not decision.allowed


def test_apply_patch_needs_approval_when_enabled():
    engine = PolicyEngine(Policy(allow_patch=True))
    decision = engine.check(_tool("apply_patch", Risk.DANGEROUS), {})
    assert decision.verdict is Verdict.APPROVE
    assert decision.requires_approval


def test_commit_denied_by_default():
    engine = PolicyEngine(Policy())
    assert engine.check(_tool("git_commit", Risk.DANGEROUS), {}).verdict is Verdict.DENY


def test_destructive_git_is_not_a_tool_at_all():
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    repo = load_repo_config(REPO / "fixtures" / "cpp_sandbox")
    registry, _, _ = build_registry(repo)
    forbidden = {"git_push", "git_reset", "git_clean", "git_checkout", "run_shell",
                 "git_rebase", "git_merge", "git_bisect"}
    assert forbidden.isdisjoint(set(registry.names()))


# ------------------------------------------------------------------ skills


def test_frontmatter_folded_scalar_and_list():
    meta, body = _parse_frontmatter(
        "---\n"
        "name: demo\n"
        "description: >\n"
        "  First line.\n"
        "  Second line.\n"
        "tools: [read_file, search_text]\n"
        "---\n"
        "# Body\n"
    )
    assert meta["name"] == "demo"
    assert meta["description"] == "First line. Second line."
    assert meta["tools"] == ["read_file", "search_text"]
    assert body.strip() == "# Body"


def test_skills_discovered_with_required_frontmatter():
    library = SkillLibrary.discover(REPO / ".github" / "skills")
    assert len(library) >= 6
    for name in library.names():
        skill = library.get(name)
        assert skill is not None
        assert skill.name and skill.description
        assert len(skill.body.splitlines()) < 500, f"{name} SKILL.md is too long"


def test_catalogue_is_cheap():
    library = SkillLibrary.discover(REPO / ".github" / "skills")
    # Roughly 100 tokens per skill is the disclosure budget.
    assert len(library.catalogue()) / 4 < 100 * len(library)


def test_routing_picks_the_obvious_skill():
    library = SkillLibrary.discover(REPO / ".github" / "skills")
    cases = {
        "the build is failing with an undefined reference": "diagnose-build-failure",
        "why is the ring_buffer test failing": "diagnose-test-failure",
        "review my uncommitted changes": "git-review",
        "write me a commit message for this": "prepare-commit",
        "where does RingBuffer live in this repository": "repo-navigation",
    }
    for task, expected in cases.items():
        assert library.route(task) == expected, (task, library.rank(task))


def test_declared_tools_all_exist():
    from local_agent.config import load_repo_config
    from local_agent.tools import build_registry

    repo = load_repo_config(REPO / "fixtures" / "cpp_sandbox")
    registry, _, _ = build_registry(repo)
    library = SkillLibrary.discover(REPO / ".github" / "skills")
    for name in library.names():
        skill = library.get(name)
        assert skill is not None
        for tool in skill.tools:
            assert tool in registry, f"{name} declares unknown tool {tool}"
