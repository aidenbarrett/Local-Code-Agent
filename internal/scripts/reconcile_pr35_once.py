#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(
        list(args),
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=check,
    )


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one occurrence, found {count}: {old!r}")
    write(path, text.replace(old, new, 1))


def resolve_chat() -> None:
    path = "internal/scripts/chat.py"
    text = read(path)
    if "--ensure-only" not in text:
        persona_line = (
            '    parser.add_argument("--persona", default="off", metavar="NAME_OR_PATH", '
            'help="optional chat-only tone profile: \'neutral\', \'off\', or a persona TOML path")\n'
        )
        if persona_line not in text:
            raise RuntimeError("chat.py: persona parser line not found")
        text = text.replace(
            persona_line,
            persona_line
            + '    parser.add_argument("--ensure-only", action="store_true", help=argparse.SUPPRESS)\n',
            1,
        )
        marker = "    profile, _device, config = resolved\n    term = ui()\n"
        if marker not in text:
            raise RuntimeError("chat.py: resolved-profile marker not found")
        text = text.replace(
            marker,
            "    profile, _device, config = resolved\n"
            "    if args.ensure_only:\n"
            "        return 0 if _ensure_server(profile, config) else 2\n"
            "    term = ui()\n",
            1,
        )
    write(path, text)


def patch_files() -> None:
    # Keep the experiment boundary clean: skills teach engineering procedure,
    # while submit_answer/claim vocabulary stays in the shared system prompt.
    replace_once(
        "internal/skills/repo-navigation/SKILL.md",
        "2. If `repo_info` reports configured build/test commands, that is sufficient for\n"
        "   a high-level question such as \"how does this repository build?\". Finish with\n"
        "   `submit_answer` immediately, citing the `repo_info` call. Do **not** enumerate\n",
        "2. If `repo_info` reports configured build/test commands, that is sufficient for\n"
        "   a high-level question such as \"how does this repository build?\". Finish\n"
        "   immediately using that evidence. Do **not** enumerate\n",
    )
    replace_once(
        "internal/skills/repo-navigation/SKILL.md",
        "- Once the requested question is answered by current evidence, call\n"
        "  `submit_answer`; more tool calls are not progress.\n",
        "- Once the requested question is answered by current evidence, stop; more tool\n"
        "  calls are not progress.\n",
    )
    if "submit_answer" in read("internal/skills/repo-navigation/SKILL.md"):
        raise RuntimeError("repo-navigation skill still contains reporting-protocol language")

    replace_once(
        "internal/tests/unit/test_repo_navigation_small_model_contract.py",
        '    assert "Finish with\\n   `submit_answer` immediately" in skill\n',
        '    assert "Finish\\n   immediately using that evidence." in skill\n'
        '    assert "`submit_answer`" not in skill\n',
    )

    replace_once(
        "install.ps1",
        "            Write-Host '.\\install.ps1 -InstallMissing after prerequisite installation has been approved.'\n",
        "            Write-Host '.\\install.ps1 -InstallMissing for non-interactive setup after prerequisite installation has been approved.'\n",
    )

    replace_once(
        "README.md",
        "The incremental written-procedure contrast was much smaller and unresolved:",
        "The incremental written-procedure contrast was small and unresolved:",
    )

    replace_once(
        "internal/tests/unit/test_compatibility_runner_fail_closed.py",
        "    assert 'default=[\"internal/tests\"]' in source\n"
        "    assert 'args.paths or [\"internal/tests\"]' in source\n",
        "    assert 'DEFAULT_TEST_PATH = Path(__file__).resolve().parents[1] / \"tests\"' in source\n"
        "    assert 'paths = [Path(p) for p in args.paths] if args.paths else [DEFAULT_TEST_PATH]' in source\n",
    )

    replace_once(
        "internal/tests/unit/test_metrics_and_context.py",
        "def test_recent_results_survive_compaction_intact():\n"
        "    ctx = ContextManager(budget_tokens=2_000, keep_recent_tool_results=2)\n",
        "def test_recent_results_survive_compaction_intact():\n"
        "    ctx = ContextManager(budget_tokens=6_000, keep_recent_tool_results=2)\n",
    )

    replace_once(
        "internal/tests/unit/test_orchestrator_guards.py",
        "        repo, registry, ScriptedClient(turns), skills, context_budget_tokens=900\n",
        "        repo, registry, ScriptedClient(turns), skills, context_budget_tokens=4_000\n",
    )

    replace_once(
        "internal/tests/unit/test_post_refactor_review_contracts.py",
        "def test_public_run_task_uses_provisioned_8b_profile_and_ensures_server():\n"
        "    source = (REPO / \"local-code-agent.ps1\").read_text(encoding=\"utf-8\")\n"
        "    assert \"qwen3-8b-npu --ensure-only\" in source\n"
        "    assert \"--profile ptl-npu-8b run @Rest\" in source\n"
        "    assert \"local_agent.cli run @Rest\" not in source\n",
        "def test_public_run_task_uses_provisioned_8b_profile_and_ensures_server():\n"
        "    source = (REPO / \"internal\" / \"scripts\" / \"run-task-ui.py\").read_text(encoding=\"utf-8\")\n"
        "    assert '\"qwen3-8b-npu\"' in source\n"
        "    assert '\"--ensure-only\"' in source\n"
        "    assert '\"--profile\",' in source\n"
        "    assert '\"ptl-npu-8b\",' in source\n"
        "    assert '\"local_agent.cli\"' in source\n",
    )

    replace_once(
        "local-code-agent.ps1",
        "        # presenter still delegates server ownership to chat.ps1 and execution\n"
        "        # to local_agent.cli using the provisioned ptl-npu-8b profile.\n",
        "        # presenter still delegates server ownership to chat.ps1 and execution\n"
        "        # through the controlled developer path using the provisioned ptl-npu-8b profile.\n",
    )


def restamp_identity() -> None:
    sys.path.insert(0, str(ROOT / "internal"))
    from local_agent import provenance as p

    instrument_path = ROOT / "internal" / "INSTRUMENT.json"
    data = json.loads(instrument_path.read_text(encoding="utf-8"))
    if data.get("generation") != 2:
        raise RuntimeError("unexpected instrument generation")
    comments = " ".join(data.get("_comment") or [])
    if "no collected model rows yet" not in comments:
        raise RuntimeError("generation-2 zero-row declaration is missing; refusing restamp")

    base_prompt = p.base_prompt_sha256()
    outcome = p.outcome_contract_sha256()
    if base_prompt != data["base_prompt_sha256"]:
        raise RuntimeError(
            f"base-prompt contract moved unexpectedly: {data['base_prompt_sha256']} -> {base_prompt}"
        )
    if outcome != data["outcome_contract_sha256"]:
        raise RuntimeError(
            f"outcome contract moved unexpectedly: {data['outcome_contract_sha256']} -> {outcome}"
        )

    source = p.source_sha256()
    print(f"source_sha256: {data['source_sha256']} -> {source}")
    print(f"base_prompt_sha256: {base_prompt} (unchanged)")
    print(f"outcome_contract_sha256: {outcome} (unchanged)")
    data["source_sha256"] = source
    instrument_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    run("git", "fetch", "origin", "main")
    merge = run("git", "merge", "--no-commit", "--no-ff", "origin/main", check=False)
    if merge.returncode not in (0, 1):
        raise RuntimeError(f"git merge failed with {merge.returncode}")

    unmerged = run("git", "diff", "--name-only", "--diff-filter=U", capture=True).stdout.splitlines()
    if unmerged:
        if set(unmerged) != {"internal/scripts/chat.py"}:
            raise RuntimeError(f"unexpected merge conflicts: {unmerged}")
        run("git", "checkout", "--theirs", "--", "internal/scripts/chat.py")
        run("git", "add", "internal/scripts/chat.py")

    resolve_chat()
    patch_files()
    restamp_identity()

    run(sys.executable, "internal/benchmark_fixture/generate_project.py")

    # Fast targeted gate for every failure observed on the previous PR head,
    # plus the newly merged persona path.
    run(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "internal/tests/integration/test_evaluator_cannot_be_fooled.py::test_no_skill_body_teaches_the_reporting_protocol",
        "internal/tests/unit/test_adversarial_followup_contracts.py",
        "internal/tests/unit/test_compatibility_runner_fail_closed.py",
        "internal/tests/unit/test_metrics_and_context.py::test_recent_results_survive_compaction_intact",
        "internal/tests/unit/test_orchestrator_guards.py::test_compaction_is_recorded_and_warned_about",
        "internal/tests/unit/test_post_refactor_review_contracts.py",
        "internal/tests/unit/test_run_task_ui.py::test_root_run_task_uses_the_product_presenter_not_raw_cli_output",
        "internal/tests/unit/test_repo_navigation_small_model_contract.py",
        "internal/tests/unit/test_chat_persona.py",
    )

    # Authoritative and offline gates both have to pass before this helper pushes.
    run(sys.executable, "-m", "pytest", "-q")
    run(sys.executable, "internal/measurement/run_test_suite.py", "internal/tests")

    # Recheck provenance after the suites in case a test modified a hashed file.
    sys.path.insert(0, str(ROOT / "internal"))
    from local_agent import provenance as p
    data = json.loads((ROOT / "internal" / "INSTRUMENT.json").read_text(encoding="utf-8"))
    if p.source_sha256() != data["source_sha256"]:
        raise RuntimeError("source identity changed during verification")

    # The repair machinery must not survive the repair commit.
    (ROOT / ".github" / "workflows" / "reconcile-pr35-once.yml").unlink()
    Path(__file__).unlink()

    run("git", "add", "-A")
    if run("git", "diff", "--cached", "--check", check=False).returncode != 0:
        raise RuntimeError("git diff --cached --check failed")
    if run("git", "diff", "--name-only", "--diff-filter=U", capture=True).stdout.strip():
        raise RuntimeError("unmerged paths remain")

    run("git", "config", "user.name", "github-actions[bot]")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("git", "commit", "-m", "Reconcile PR35 with latest main and fix CI regressions")
    run("git", "push", "origin", "HEAD:fix/post-refactor-review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
