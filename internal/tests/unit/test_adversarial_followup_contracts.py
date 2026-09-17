from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_public_run_task_uses_the_profile_installed_by_first_run_setup():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    presenter = (REPO / "internal" / "scripts" / "run-task-ui.py").read_text(encoding="utf-8")
    run_task_block = wrapper.split("'run-task' {", 1)[1].split("'verification-demo' {", 1)[0]
    assert "scripts\\run-task-ui.py" in run_task_block
    assert '"ptl-npu-8b"' in presenter
    assert '"qwen3-8b-npu"' in presenter
    assert "local_agent.cli" in presenter


def test_repository_self_profile_uses_windows_compatible_python_command():
    text = (REPO / ".local-agent.toml").read_text(encoding="utf-8")
    assert 'build = ["python",' in text
    assert 'test = ["python",' in text
    assert '"python3"' not in text


def test_stale_evidence_demo_does_not_forward_a_null_argument():
    text = (REPO / "demo" / "show-stale-test-rejection.ps1").read_text(encoding="utf-8")
    assert "if ($Rest)" in text
    assert "else { & $entry verification-demo }" in text


def test_windows_wrappers_handle_runtime_location_and_noninteractive_setup_explicitly():
    chat = (REPO / "chat.ps1").read_text(encoding="utf-8")
    install = (REPO / "install.ps1").read_text(encoding="utf-8")
    assert "$env:LOCALAPPDATA" in chat
    assert "GetFolderPath" in chat
    assert "Join-Path $HOME '.local'" in chat
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
    assert "NUC under WSL2 Ubuntu with llama.cpp" in text
    assert "not independent tasks" in text
