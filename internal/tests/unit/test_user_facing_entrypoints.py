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
        "local-code-agent.ps1",
        "README.md",
        "QUICKSTART.md",
        "demo",
        "internal",
    ):
        assert (REPO / name).exists(), name
    assert not (REPO / "chat.ps1").exists()

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


def test_raw_chat_is_subordinate_to_the_canonical_launcher():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "[string]$Command = 'session'" in wrapper
    assert "'chat' {" in wrapper
    assert "Set-ManagedOvmsEnvironment" in wrapper
    assert "LCA_OVMS_EXECUTABLE" in wrapper
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


def test_chat_is_one_product_subcommand_and_never_teaches_old_plumbing():
    source = (INTERNAL / "scripts" / "chat.py").read_text(encoding="utf-8")
    assert ".\\local-code-agent.ps1 chat qwen3-8b-npu" in source
    assert ".\\chat.ps1" not in source
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
    assert "raw terminal-chat mode" in message
    assert "no network access" in message
    assert "no tools" in message
    assert "no access to the filesystem" in message
    assert "Session Hub" in message
    assert "does not have those capabilities" in message
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



def test_local_code_agent_root_facade_is_the_default_product_surface():
    wrapper = (REPO / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "Canonical user-facing entrypoint" in wrapper
    assert "[string]$Command = 'session'" in wrapper
    assert "capabilities" in wrapper
    assert "run-task" in wrapper
    assert "verification-demo" in wrapper
    assert "$internal = Join-Path $root 'internal'" in wrapper
    assert "(Join-Path $internal 'scripts\\session-hub.py')" in wrapper
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
        (("chat",), "Available local model choices"),
        (("help",), "Session Hub"),
    )
    for args, expected in cases:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(checkout / "local-code-agent.ps1"),
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


