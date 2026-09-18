from pathlib import Path
import tomllib


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
    config = tomllib.loads((REPO / ".local-agent.toml").read_text(encoding="utf-8"))
    profile_name = config["repo"]["default_profile"]
    profile = config["profiles"][profile_name]
    assert profile["build"][0] == "python"
    assert profile["test"][0] == "python"
    assert all(arg != "python3" for command in (profile["build"], profile["test"]) for arg in command)


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


def _workflow_trigger_block(path: Path, trigger: str) -> list[str]:
    """Parse the small top-level `on:` YAML subset without pinning whitespace."""
    lines = path.read_text(encoding="utf-8").splitlines()
    on_index = next(
        i for i, line in enumerate(lines)
        if line.strip() == "on:" and not line.startswith((" ", "\t"))
    )
    end = len(lines)
    for i in range(on_index + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith((" ", "\t", "#")):
            end = i
            break
    on_lines = lines[on_index + 1:end]
    for i, line in enumerate(on_lines):
        if line.strip() == f"{trigger}:":
            indent = len(line) - len(line.lstrip())
            block = []
            for child in on_lines[i + 1:]:
                if child.strip() and len(child) - len(child.lstrip()) <= indent:
                    break
                block.append(child.strip())
            return block
    raise AssertionError(f"workflow has no {trigger!r} trigger")


def test_serving_power_shell_gate_also_runs_after_merge_to_main():
    block = _workflow_trigger_block(REPO / ".github" / "workflows" / "serving.yml", "push")
    compact = " ".join(block).replace(" ", "")
    assert "branches:[main]" in compact or any(line == "- main" for line in block)


# The README replication table used to be checked here with literal cell strings.
# `test_public_claims_match_frozen_evidence.py` now recomputes those public claims
# from the frozen rows and separately pins the prose-only caveats.
