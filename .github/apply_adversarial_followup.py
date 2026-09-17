#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def write(rel: str, text: str) -> None:
    (ROOT / rel).write_text(text, encoding="utf-8", newline="\n")


def replace_once(rel: str, old: str, new: str) -> None:
    text = read(rel)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{rel}: expected one occurrence, found {count}: {old!r}")
    write(rel, text.replace(old, new, 1))


# 1. A stale ownership record must not suppress foreign-endpoint refusal.
replace_once(
    "internal/scripts/chat.py",
    "    elif _reachable(config):\n",
    "    if _reachable(config):\n",
)

chat_tests = "internal/tests/unit/test_chat_server_orchestration.py"
text = read(chat_tests)
name = "test_chat_refuses_unmanaged_server_even_with_a_stale_ownership_record"
if name not in text:
    text += r'''


def test_chat_refuses_unmanaged_server_even_with_a_stale_ownership_record(monkeypatch, tmp_path):
    chat = _chat()
    profile, _, config = _config(chat)
    plan = SimpleNamespace()
    record = {"plan": {"model_configuration": {"device": "NPU", "model": config.model}}}
    started = []

    monkeypatch.setattr(chat, "_runtime_root", lambda: tmp_path)
    monkeypatch.setattr(chat.serve, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(chat.serve, "read_record", lambda p: record)
    monkeypatch.setattr(chat.serve, "status", lambda p: {"healthy": False, "process_alive": False})
    monkeypatch.setattr(chat, "_reachable", lambda c: True)
    monkeypatch.setattr(chat.serve, "start", lambda *a, **k: started.append(True))

    assert chat._ensure_server(profile, config) is False
    assert started == []
'''
    write(chat_tests, text)

# 2. The compatibility runner must fail closed and default to the live test tree.
runner = "internal/measurement/run_test_suite.py"
replace_once(
    runner,
    "    python measurement/run_test_suite.py tests\n    python measurement/run_test_suite.py tests/unit -k policy\n",
    "    python internal/measurement/run_test_suite.py internal/tests\n    python internal/measurement/run_test_suite.py internal/tests/unit -k policy\n",
)
replace_once(runner, '    sys.path.insert(0, str(root / "src"))\n', '    sys.path.insert(0, str(root / "internal"))\n')
replace_once(
    runner,
    '''    files: list[Path] = []
    for path in (p.resolve() for p in paths):
        if path.is_file():
            files.append(path)
        else:
            files.extend(sorted(path.rglob("test_*.py")))

    conftest_fixtures: dict[str, Callable] = {}
    for conftest in sorted({f.parent for f in files} | {root / "tests"}):
''',
    '''    files: list[Path] = []
    missing: list[Path] = []
    for path in (p.resolve() for p in paths):
        if not path.exists():
            missing.append(path)
        elif path.is_file():
            files.append(path)
        else:
            files.extend(sorted(path.rglob("test_*.py")))

    if missing:
        for path in missing:
            print(f"ERROR: test path does not exist: {path}", file=sys.stderr)
        return 2
    if not files:
        joined = ", ".join(str(p) for p in paths)
        print(f"ERROR: no test files collected from: {joined}", file=sys.stderr)
        return 2

    conftest_fixtures: dict[str, Callable] = {}
    for conftest in sorted({f.parent for f in files} | {root / "internal" / "tests"}):
''',
)
replace_once(
    runner,
    '    parser.add_argument("paths", nargs="*", default=["tests"])\n',
    '    parser.add_argument("paths", nargs="*", default=["internal/tests"])\n',
)
replace_once(
    runner,
    '    return run([Path(p) for p in (args.paths or ["tests"])], args.keyword, args.verbose)\n',
    '    return run([Path(p) for p in (args.paths or ["internal/tests"])], args.keyword, args.verbose)\n',
)

write(
    "internal/tests/unit/test_compatibility_runner_fail_closed.py",
    r'''from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "internal" / "measurement" / "run_test_suite.py"


def _run(*args: str):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_compatibility_runner_default_points_at_live_test_tree():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'default=["internal/tests"]' in source
    assert 'args.paths or ["internal/tests"]' in source


def test_compatibility_runner_refuses_missing_test_path(tmp_path):
    result = _run(str(tmp_path / "missing"))
    assert result.returncode == 2
    assert "test path does not exist" in result.stderr


def test_compatibility_runner_refuses_empty_test_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(str(empty))
    assert result.returncode == 2
    assert "no test files collected" in result.stderr
''',
)

# 3. Make the Windows-only wrappers fail clearly instead of crashing/hanging.
replace_once(
    "chat.ps1",
    "$internal = Join-Path $root 'internal'\n$runtimeRoot = Join-Path $env:LOCALAPPDATA 'LocalCodeAgent'\n",
    "$internal = Join-Path $root 'internal'\n\nif ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {\n    Write-Host ''\n    Write-Host 'chat.ps1 currently supports the Windows workstation path only.'\n    Write-Host 'The controller is portable, but this wrapper expects the managed Windows OVMS layout.'\n    Write-Host ''\n    exit 2\n}\nif (-not $env:LOCALAPPDATA) {\n    Write-Host 'LOCALAPPDATA is not set; cannot locate the managed Local Code Agent runtime.'\n    exit 2\n}\n$runtimeRoot = Join-Path $env:LOCALAPPDATA 'LocalCodeAgent'\n",
)
replace_once(
    "install.ps1",
    "    if (-not $InstallMissing) {\n        $answer = Read-Host 'Continue with setup? [y/N]'\n",
    "    if (-not $InstallMissing) {\n        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {\n            Write-Host ''\n            Write-Host 'Interactive setup confirmation is unavailable in this session.'\n            Write-Host 'Run .\\install.ps1 -CheckOnly for a read-only preflight.'\n            Write-Host 'After approval, run .\\install.ps1 -InstallMissing for non-interactive setup.'\n            Write-Host ''\n            exit 2\n        }\n        $answer = Read-Host 'Continue with setup? [y/N]'\n",
)

# 4. Keep the native Windows syntax gate alive after merge, not only on PRs.
replace_once(
    ".github/workflows/serving.yml",
    "on:\n  pull_request:\n  workflow_dispatch:\n",
    "on:\n  pull_request:\n  push:\n    branches: [main]\n  workflow_dispatch:\n",
)

# 5. One canonical identity recomputation recipe.
replace_once(
    "AGENTS.md",
    'python -c "from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"',
    'python -c "import sys; sys.path.insert(0,\'internal\'); from local_agent import provenance as p; print(p.source_sha256()); print(p.base_prompt_sha256()); print(p.outcome_contract_sha256())"',
)

# 6. Put the stronger frozen replication in the public evidence section without
#    editing or reinterpreting any frozen dataset.
readme = read("README.md")
start = readme.index("## Measured evidence so far")
end = readme.index("## How it works", start)
measured = '''## Measured evidence so far

The strongest completed generation-1 evidence is the balanced three-repeat replication of the same ten synthetic C++ tasks with one local 30B model on CPU:

| Condition | Verified completion |
|---|---:|
| Control | 9/30 (0.300) |
| Narrow tools | 22/30 (0.733) |
| Narrow tools + written skill | 23/29 (0.793) |

Restricting the available action space produced the large observed movement: **+0.433** from control to narrow. Across the repeated fixture, control recorded 11 scope violations while narrow and skill recorded none. The written procedure added **+0.060** over narrow in aggregate, but the frozen findings treat that movement as small and unresolved rather than evidence of a general procedure effect. One skill row was infrastructure-invalid and is excluded from its denominator.

The earlier smoke run at the same frozen instrument identity was 3/10, 8/10 and 8/10. The three-repeat run rotated condition order and reproduced the same overall shape; the repeats are repeated observations of the same ten fixtures, **not** independent tasks or a population-level statistical claim.

These generation-1 measurements were taken on the historical NUC/WSL2 Ubuntu path using Qwen3-Coder-30B (`UD-Q4_K_XL`) through llama.cpp. They are **not** measurements of the current Panther Lake Windows/OVMS path. No 8B cell was run, so the result provides no evidence that the same effect transfers to the smaller model used in the current demo.

Frozen analysis: [`internal/experiments/2026-09-08-30b-three-conditions-x3/findings.md`](internal/experiments/2026-09-08-30b-three-conditions-x3/findings.md).

Generation 1 is reproduced from the historical tag `instrument-08d5e0fe`. The current tree intentionally has a different repository layout and source identity; recomputing the generation-1 source hash from the current tree is not a valid reproduction procedure.

'''
write("README.md", readme[:start] + measured + readme[end:])

# The x3 dataset has the same frozen generation-1 identity/tag. Add it to the
# reproduction table; do not modify its data/findings.
replace_once(
    "internal/experiments/README.md",
    "| `2026-09-08-30b-three-conditions` | `08d5e0fe...` | `instrument-08d5e0fe` |\n",
    "| `2026-09-08-30b-three-conditions` | `08d5e0fe...` | `instrument-08d5e0fe` |\n| `2026-09-08-30b-three-conditions-x3` | `08d5e0fe...` | `instrument-08d5e0fe` |\n",
)

# 7. Make historical/current documentation boundaries explicit and repair the
#    highest-value stale current commands.
project_history = read("internal/docs/project-history.md")
if project_history.startswith("# Local Code Agent\n"):
    project_history = project_history.replace(
        "# Local Code Agent\n",
        "# Historical project snapshot\n\n> **Historical document.** This preserves the generation-1/research-first project\n> framing and the repository layout that existed at that time. Paths, commands,\n> test counts and status statements below are historical, not current setup\n> instructions. For the current product surface use the root `README.md` and\n> `QUICKSTART.md`; reproduce generation 1 from `instrument-08d5e0fe`.\n",
        1,
    )
# Fix links so the historical document remains navigable from internal/docs.
project_history = project_history.replace("](experiments/", "](../experiments/")
project_history = project_history.replace("](local_agent/", "](../local_agent/")
project_history = project_history.replace("](docs/", "](")
write("internal/docs/project-history.md", project_history)

# Current internal operational docs should use current paths.
for rel in ("internal/docs/bring-up.md", "internal/docs/serving.md", "internal/docs/measurement-protocol.md"):
    text = read(rel)
    text = text.replace("python measurement/", "python internal/measurement/")
    text = text.replace("python measurement\\", "python internal/measurement\\")
    text = text.replace("python evaluation/", "python internal/evaluation/")
    text = text.replace("python evaluation\\", "python internal/evaluation\\")
    text = text.replace("python benchmark_fixture/", "python internal/benchmark_fixture/")
    text = text.replace("python benchmark_fixture\\", "python internal/benchmark_fixture\\")
    text = text.replace("local-agent --repo benchmark_fixture/", "local-agent --repo internal/benchmark_fixture/")
    text = text.replace("local-agent --repo benchmark_fixture\\", "local-agent --repo internal/benchmark_fixture\\")
    text = text.replace("cd benchmark_fixture\\", "cd internal\\benchmark_fixture\\")
    text = text.replace("python -m pytest tests/test_serving.py tests/test_energy.py", "python -m pytest internal/tests/test_serving.py internal/tests/test_energy.py")
    write(rel, text)

# Prefer the public setup entrypoint in the current serving guide.
text = read("internal/docs/serving.md")
text = text.replace(".\\scripts\\work-laptop-one-shot.ps1 -InstallMissing", ".\\install.ps1 -InstallMissing")
write("internal/docs/serving.md", text)

# Resolve the physical-validation contradiction conservatively: only the NPU is
# recorded as physically exercised; GPU/CPU and power evidence remain pending.
replace_once(
    "internal/docs/serving-and-accelerators.md",
    "The remaining work is physical validation on the Panther Lake laptop: start the\nreal NPU and GPU servers, prove inference on each intended profile, and capture\nHWiNFO power data. That is hardware evidence, not missing launcher architecture.\n",
    "The Panther Lake **NPU** path has been physically exercised on Windows. The\nremaining hardware work is to rehearse the current public path end to end,\nphysically confirm the intended GPU/CPU serving paths, and capture HWiNFO power\ndata. Those are hardware-evidence tasks, not missing launcher architecture.\n",
)

# 8. Regression contracts for the product facade and CI/doc guardrails.
write(
    "internal/tests/unit/test_adversarial_followup_contracts.py",
    r'''from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_public_run_task_uses_the_profile_installed_by_first_run_setup():
    source = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "-m local_agent.cli --profile ptl-npu-8b run @Rest" in source
    assert "-m local_agent.cli run @Rest" not in source


def test_repository_self_profile_uses_windows_compatible_python_command():
    text = (REPO / ".local-agent.toml").read_text(encoding="utf-8")
    assert 'build = ["python",' in text
    assert 'test = ["python",' in text
    assert '"python3"' not in text


def test_stale_evidence_demo_does_not_forward_a_null_argument():
    text = (REPO / "demo" / "show-stale-test-rejection.ps1").read_text(encoding="utf-8")
    assert "if ($Rest)" in text
    assert "else { & $entry verification-demo }" in text


def test_windows_wrappers_fail_explicitly_in_unsupported_or_noninteractive_contexts():
    chat = (REPO / "chat.ps1").read_text(encoding="utf-8")
    install = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "supports the Windows workstation path only" in chat
    assert "LOCALAPPDATA is not set" in chat
    assert "[Console]::IsInputRedirected" in install
    assert "-InstallMissing for non-interactive setup" in install


def test_serving_power_shell_gate_also_runs_after_merge_to_main():
    text = (REPO / ".github" / "workflows" / "serving.yml").read_text(encoding="utf-8")
    assert "push:\n    branches: [main]" in text


def test_public_readme_uses_the_balanced_three_repeat_generation_one_result():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "9/30 (0.300)" in text
    assert "22/30 (0.733)" in text
    assert "23/29 (0.793)" in text
    assert "small and unresolved" in text
    assert "NUC/WSL2 Ubuntu" in text
    assert "not independent tasks" in text
''',
)

# 9. Recompute the declared identities from the exact patched tree. Only the
#    source axis is expected to move for the runner hardening.
sys.path.insert(0, str(ROOT / "internal"))
from local_agent import provenance as p  # noqa: E402

instrument_path = ROOT / "internal" / "INSTRUMENT.json"
instrument = json.loads(instrument_path.read_text(encoding="utf-8"))
old_base = instrument["base_prompt_sha256"]
old_outcome = instrument["outcome_contract_sha256"]
new_source = p.source_sha256()
new_base = p.base_prompt_sha256()
new_outcome = p.outcome_contract_sha256()
if new_base != old_base:
    raise RuntimeError(f"unexpected model-facing identity move: {old_base} -> {new_base}")
if new_outcome != old_outcome:
    raise RuntimeError(f"unexpected outcome identity move: {old_outcome} -> {new_outcome}")
instrument["source_sha256"] = new_source
instrument["base_prompt_sha256"] = new_base
instrument["outcome_contract_sha256"] = new_outcome
instrument_path.write_text(json.dumps(instrument, indent=2) + "\n", encoding="utf-8", newline="\n")
print(f"source_sha256={new_source}")
print(f"base_prompt_sha256={new_base}")
print(f"outcome_contract_sha256={new_outcome}")

# The patch driver and workflow are one-shot scaffolding. Remove both before the
# final commit so none of this repair machinery can merge to main.
(ROOT / ".github" / "apply_adversarial_followup.py").unlink()
(ROOT / ".github" / "workflows" / "apply-adversarial-followup.yml").unlink()
