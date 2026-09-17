from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_public_run_task_uses_provisioned_8b_profile_and_ensures_server():
    source = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "qwen3-8b-npu --ensure-only" in source
    assert "--profile ptl-npu-8b run @Rest" in source
    assert "local_agent.cli run @Rest" not in source


def test_repository_python_profile_is_windows_portable():
    source = (REPO / ".local-agent.toml").read_text(encoding="utf-8")
    assert '["python", "-c"' in source
    assert '["python", "internal/measurement/run_test_suite.py", "internal/tests"]' in source
    assert '"python3"' not in source


def test_stale_test_demo_does_not_splat_null_arguments():
    source = (REPO / "demo" / "show-stale-test-rejection.ps1").read_text(encoding="utf-8")
    assert "if ($Rest)" in source
    assert "else { & $entry verification-demo }" in source


def test_bare_install_refuses_noninteractive_prompting():
    source = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "[Environment]::UserInteractive" in source
    assert "[Console]::IsInputRedirected" in source
    assert "requires an interactive confirmation" in source
    assert ".\\install.ps1 -InstallMissing" in source
    assert ".\\install.ps1 -CheckOnly" in source


def test_chat_runtime_root_does_not_require_localappdata():
    source = (REPO / "chat.ps1").read_text(encoding="utf-8")
    assert "$localAppData = $env:LOCALAPPDATA" in source
    assert "[Environment+SpecialFolder]::LocalApplicationData" in source
    assert "$runtimeRoot = Join-Path $localAppData 'LocalCodeAgent'" in source


def test_serving_gate_runs_on_main_pushes():
    source = (REPO / ".github" / "workflows" / "serving.yml").read_text(encoding="utf-8")
    assert "push:\n    branches: [main]" in source
    assert "pull_request:" in source


def test_public_evidence_uses_balanced_generation_one_replication():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "9/30 (0.300)" in readme
    assert "22/30 (0.733)" in readme
    assert "23/29 (0.793)" in readme
    assert "no clear aggregate procedure effect" in readme
    assert "NUC under WSL2 Ubuntu with llama.cpp" in readme
    assert "not measurements of the current Panther Lake / Windows / OVMS / Qwen3-8B demo stack" in readme


def test_repeated_dataset_points_to_frozen_generation_one_tag():
    experiments = (REPO / "internal" / "experiments" / "README.md").read_text(encoding="utf-8")
    assert "2026-09-08-30b-three-conditions-x3` | `08d5e0fe...` | `instrument-08d5e0fe`" in experiments
