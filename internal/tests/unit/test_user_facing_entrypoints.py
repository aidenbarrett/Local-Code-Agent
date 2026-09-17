import builtins
import contextlib
from importlib.util import module_from_spec, spec_from_file_location
import io
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"


def _load_chat_module():
    script = INTERNAL / "scripts" / "chat.py"
    spec = spec_from_file_location("user_chat", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_product_root_is_small_and_implementation_is_hidden():
    for name in (
        "install.ps1",
        "chat.ps1",
        "local-code-agent.ps1",
        "README.md",
        "QUICKSTART.md",
        "demo",
        "internal",
    ):
        assert (REPO / name).exists(), name

    for old_root in (
        "local_agent",
        "measurement",
        "evaluation",
        "scripts",
        "tests",
        "experiments",
        "benchmark_fixture",
        "docs",
        "skills",
    ):
        assert not (REPO / old_root).exists(), old_root


def test_root_chat_entrypoint_and_friendly_models_are_present():
    wrapper = (REPO / "chat.ps1").read_text(encoding="utf-8")
    assert "$internal = Join-Path $root 'internal'" in wrapper
    assert "(Join-Path $internal 'scripts\\chat.py')" in wrapper
    assert "@Rest" in wrapper
    assert "@Args" not in wrapper

    chat = _load_chat_module()
    assert chat.FRIENDLY == {
        "qwen3-8b-npu": ("ptl-npu-8b", "NPU"),
        "qwen3-8b-gpu": ("ptl-npu-8b", "GPU"),
        "qwen3-8b-cpu": ("ptl-npu-8b", "CPU"),
    }
    assert "qwen3-coder-30b" not in chat.FRIENDLY


def test_chat_is_one_command_and_never_teaches_internal_plumbing():
    source = (INTERNAL / "scripts" / "chat.py").read_text(encoding="utf-8")
    assert ".\\chat.ps1 qwen3-8b-npu" in source
    assert "serve.start(plan, config" in source
    assert "serve.read_record(plan)" in source
    assert "serve.status(plan)" in source
    assert "Run the root setup command" in source
    assert ".\\install.ps1" in source
    assert ".\\scripts\\demo-accelerator.ps1" not in source
    assert "python measurement/serve.py" not in source
    assert "Available local model choices" in source


def test_direct_chat_system_message_describes_the_real_terminal_boundary():
    chat = _load_chat_module()
    _, _, config = chat._resolve("qwen3-8b-npu")
    message = chat._system_message(config)["content"]
    assert "plain terminal chat program" in message
    assert "Ctrl-C while at the prompt" in message
    assert "no network access" in message
    assert "no tools" in message
    assert "no access to the filesystem" in message
    assert "separate from Local Code Agent" in message
    assert "Do not guess at feature names, buttons or commands" in message
    assert "OpenVINO Model Server" in message
    assert "NPU" in message


def test_ctrl_c_during_generation_returns_cleanly_to_the_prompt(monkeypatch):
    chat = _load_chat_module()
    profile, _, config = chat._resolve("qwen3-8b-npu")
    monkeypatch.setattr(chat, "_ensure_server", lambda *_args, **_kwargs: True)

    responses = iter(["hello", ""])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(responses))

    from local_agent.llm import client as client_module

    class InterruptingClient:
        def __init__(self, _config):
            pass

        def chat(self, history):
            assert history[-1]["content"] == "hello"
            raise KeyboardInterrupt()

    monkeypatch.setattr(client_module, "OpenAICompatibleClient", InterruptingClient)

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        assert chat.converse("qwen3-8b-npu", profile, config) == 0
    assert "Generation stopped. Back at the prompt." in captured.getvalue()


def test_install_default_is_transparent_before_machine_mutation():
    source = (REPO / "install.ps1").read_text(encoding="utf-8")
    for package in (
        "Git.Git",
        "Python.Python.3.12",
        "Kitware.CMake",
        "Microsoft.VisualStudio.2022.BuildTools",
    ):
        assert package in source
    assert 'Package source: WinGet source "winget".' in source
    assert "Read-Host 'Continue with setup? [y/N]'" in source
    assert "$allowInstallMissing = $true" in source
    assert "company policy" in source
    assert ".\\install.ps1 -CheckOnly" in source
    assert ".\\install.ps1 -InstallMissing" in source


def test_readme_answers_first_time_user_questions_before_deep_internals():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    plain = " ".join(text.replace("**", "").split())
    assert "lets a local AI model work on a code repository using controlled tools" in text
    assert "## Current setup target" in text
    assert "Windows 11 on Intel Panther Lake" in text
    assert "not a claim that arbitrary Windows, Linux or macOS machines" in plain
    assert ".\\install.ps1 -CheckOnly" in text
    assert ".\\chat.ps1 qwen3-8b-npu" in text
    assert ".\\local-code-agent.ps1 capabilities" in text
    assert "Examples of intended workloads include" in text
    assert "not claims that every task is solved successfully" in text
    assert "See [`QUICKSTART.md`](QUICKSTART.md) for the full walkthrough" in text


def test_local_code_agent_root_facade_explains_why_it_exists():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "controlled repository access" in wrapper
    assert "capabilities" in wrapper
    assert "run-task" in wrapper
    assert "verification-demo" in wrapper
    assert "$internal = Join-Path $root 'internal'" in wrapper
    assert "(Join-Path $internal 'scripts\\capabilities.py')" in wrapper


def test_capabilities_use_plain_user_facing_language():
    source = (INTERNAL / "scripts" / "capabilities.py").read_text(encoding="utf-8")
    assert "Approved tools" in source
    assert "independently check" in source
    assert "typed registry" not in source
    assert "judged by the harness" not in source
    assert ".\\local-code-agent.ps1 verification-demo" in source
    assert ".\\install.ps1" in source
    assert "python benchmark_fixture/" not in source


def test_public_demo_wrappers_exist_and_hide_implementation_paths_from_docs():
    expected = {
        "run-qwen-on-npu.ps1",
        "run-qwen-on-gpu.ps1",
        "run-qwen-on-cpu.ps1",
        "show-stale-test-rejection.ps1",
        "run-complete-local-code-agent-demo.ps1",
    }
    assert expected <= {p.name for p in (REPO / "demo").iterdir()}

    quickstart = (REPO / "QUICKSTART.md").read_text(encoding="utf-8")
    assert ".\\demo\\run-qwen-on-npu.ps1" in quickstart
    assert ".\\demo\\show-stale-test-rejection.ps1" in quickstart
    assert ".\\scripts\\" not in quickstart
    assert "python measurement/" not in quickstart
    assert "qwen3-coder-30b" not in quickstart


def test_quickstart_teaches_the_detailed_user_journey_in_order():
    text = (REPO / "QUICKSTART.md").read_text(encoding="utf-8")
    clone = text.index("git clone https://github.com/aidenbarrett/Local-Code-Agent.git")
    preflight = text.index(".\\install.ps1 -CheckOnly")
    setup = text.index(".\\install.ps1\n", preflight)
    chat = text.index(".\\chat.ps1 qwen3-8b-npu")
    capabilities = text.index(".\\local-code-agent.ps1 capabilities")
    task = text.index(".\\local-code-agent.ps1 run-task")
    accelerator = text.index(".\\demo\\run-qwen-on-npu.ps1")
    verification = text.index(".\\demo\\show-stale-test-rejection.ps1")
    assert clone < preflight < setup < chat < capabilities < task < accelerator < verification
    assert "The model can propose actions. It cannot mark its own homework." in text
    assert "Windows 11 on Intel Panther Lake" in text
    assert "What are you?" in text
    assert "Are you connected to the internet?" in text
    assert "Can you inspect this repository for me?" in text


def test_generation_one_reproduction_points_to_the_frozen_tag():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "instrument-08d5e0fe" in readme
    assert "current tree intentionally has a different repository layout and source identity" in readme


@pytest.mark.skipif(os.name != "nt", reason="PowerShell path-with-spaces regression is Windows-specific")
def test_root_commands_work_from_a_checkout_path_with_spaces(tmp_path):
    checkout = tmp_path / "Local Code Agent With Spaces"
    shutil.copytree(
        REPO,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".venv-workstation",
            ".pytest_cache",
            "__pycache__",
        ),
    )

    powershell = (
        shutil.which("pwsh")
        or shutil.which("powershell.exe")
        or shutil.which("powershell")
    )
    assert powershell, "a Windows CI runner must provide PowerShell"

    cases = (
        ("chat.ps1", (), "Available local model choices"),
        ("local-code-agent.ps1", ("help",), "Local Code Agent"),
    )
    for script, args, expected in cases:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(checkout / script),
                *args,
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert expected in combined


def test_previous_research_readme_is_preserved():
    history = (INTERNAL / "docs" / "project-history.md").read_text(encoding="utf-8")
    plain = " ".join(history.replace("**", "").split()).lower()
    assert "measurement instrument first and an agent second" in plain
    assert "| verified completion | 3/10 | 8/10 | 8/10 |" in plain
