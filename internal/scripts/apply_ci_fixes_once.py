#!/usr/bin/env python3
"""One-shot repair for stale CI expectations after the internal/ layout move.

This file is temporary. It changes only tests, user-facing helper scripts and the
unhashed generation-2 identity declaration. It does not alter frozen evidence,
evaluator semantics, task contracts or the hashed agent implementation.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    target = REPO / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise SystemExit(
            f"{path}: expected {expected} occurrence(s), found {count}: {old!r}"
        )
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text.replace(old, new))


def apply() -> None:
    metrics = "internal/tests/unit/test_metrics_and_context.py"
    replace(
        metrics,
        'prov._ROOT / "benchmark_fixture" / "cpp_project" / ".local-agent"',
        'prov._ROOT / "internal" / "benchmark_fixture" / "cpp_project" / ".local-agent"',
        expected=2,
    )
    replace(
        metrics,
        '''    for expected in ("measurement/qualify_server.py", "measurement/run_experiment.sh",
                     "evaluation/run_evaluation.py", "evaluation/task_contracts.py",
                     "local_agent/agent/orchestrator.py",
                     "skills/diagnose-test-failure/SKILL.md"):
''',
        '''    for expected in ("internal/measurement/qualify_server.py",
                     "internal/measurement/run_experiment.sh",
                     "internal/evaluation/run_evaluation.py",
                     "internal/evaluation/task_contracts.py",
                     "internal/local_agent/agent/orchestrator.py",
                     "internal/skills/diagnose-test-failure/SKILL.md",
                     "pyproject.toml"):
''',
    )
    replace(
        metrics,
        '_ROOT / "benchmark_fixture" / "generate_project.py"',
        '_ROOT / "internal" / "benchmark_fixture" / "generate_project.py"',
        expected=2,
    )
    replace(
        metrics,
        '`benchmark_fixture/cpp_project` is inside',
        '`internal/benchmark_fixture/cpp_project` is inside',
    )
    replace(
        metrics,
        '''    root = Path(provenance.__file__).resolve().parent.parent
    declaration = root / "INSTRUMENT.json"
    workflow = root / ".github" / "workflows" / "tests.yml"
''',
        '''    repo_root = Path(provenance.__file__).resolve().parents[2]
    declaration = repo_root / "internal" / "INSTRUMENT.json"
    workflow = repo_root / ".github" / "workflows" / "tests.yml"
''',
    )
    replace(
        metrics,
        '''    root = Path(provenance.__file__).resolve().parent.parent
    declared = json.loads((root / "INSTRUMENT.json").read_text())
''',
        '''    repo_root = Path(provenance.__file__).resolve().parents[2]
    declared = json.loads((repo_root / "internal" / "INSTRUMENT.json").read_text())
''',
    )

    entrypoints = "internal/tests/unit/test_user_facing_entrypoints.py"
    replace(
        entrypoints,
        '''    assert "internal\\\\scripts\\\\chat.py" in wrapper
    assert "$Args" not in wrapper
''',
        '''    assert "$internal = Join-Path $root 'internal'" in wrapper
    assert "(Join-Path $internal 'scripts\\\\chat.py')" in wrapper
    assert "@Rest" in wrapper
    assert "@Args" not in wrapper
''',
    )
    replace(
        entrypoints,
        '''    assert "internal\\\\scripts\\\\capabilities.py" in wrapper
''',
        '''    assert "$internal = Join-Path $root 'internal'" in wrapper
    assert "(Join-Path $internal 'scripts\\\\capabilities.py')" in wrapper
''',
    )
    replace(
        entrypoints,
        '''    assert ".\\\\local-code-agent.ps1 verification-demo" in source
''',
        '''    assert ".\\\\local-code-agent.ps1 verification-demo" in source
    assert ".\\\\install.ps1" in source
    assert "python benchmark_fixture/" not in source
''',
    )
    replace(
        entrypoints,
        '''def test_previous_research_readme_is_preserved():
    history = (INTERNAL / "docs" / "project-history.md").read_text(encoding="utf-8")
    assert "measurement instrument first and an agent second" in history
    assert "verified completion** | **3/10** | **8/10** | **8/10**" in history
''',
        '''def test_previous_research_readme_is_preserved():
    history = (INTERNAL / "docs" / "project-history.md").read_text(encoding="utf-8")
    plain = " ".join(history.replace("**", "").split())
    assert "measurement instrument first and an agent second" in plain
    assert "| Verified completion | 3/10 | 8/10 | 8/10 |" in plain
''',
    )

    replace(
        "internal/tests/unit/test_windows_bootstrap_progress.py",
        'CORE = ROOT / "scripts" / "bootstrap-work-laptop-core.ps1"',
        'CORE = ROOT / "bootstrap-work-laptop-core.ps1"',
    )

    replace(
        "internal/scripts/chat.py",
        'print("  .\\\\install.ps1", file=sys.stderr)',
        'print(r"  .\\install.ps1", file=sys.stderr)',
    )

    replace(
        "internal/scripts/capabilities.py",
        '''        print("Create it with:", file=sys.stderr)
        print("  python benchmark_fixture/generate_project.py\\n", file=sys.stderr)
''',
        '''        print("Run the root setup command:", file=sys.stderr)
        print(r"  .\\install.ps1", file=sys.stderr)
        print(file=sys.stderr)
''',
    )
    replace(
        "internal/scripts/capabilities.py",
        'print("  .\\\\local-code-agent.ps1 verification-demo")',
        'print(r"  .\\local-code-agent.ps1 verification-demo")',
    )


def regenerate_and_restamp() -> None:
    subprocess.run(
        [sys.executable, str(REPO / "internal" / "benchmark_fixture" / "generate_project.py")],
        cwd=REPO,
        check=True,
    )
    sys.path.insert(0, str(REPO / "internal"))
    from local_agent import provenance as p

    declaration = REPO / "internal" / "INSTRUMENT.json"
    data = json.loads(declaration.read_text(encoding="utf-8"))
    data["source_sha256"] = p.source_sha256()
    data["base_prompt_sha256"] = p.base_prompt_sha256()
    data["outcome_contract_sha256"] = p.outcome_contract_sha256()
    data["how_to_recompute"] = (
        "python -c \"import sys; sys.path.insert(0,'internal'); "
        "from local_agent import provenance as p; print(p.source_sha256()); "
        "print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())\""
    )
    with declaration.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    print(data["source_sha256"])
    print(data["base_prompt_sha256"])
    print(data["outcome_contract_sha256"])


if __name__ == "__main__":
    apply()
    regenerate_and_restamp()
