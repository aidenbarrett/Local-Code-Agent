from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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
        "qwen3-coder-30b": ("ptl-gpu-30b", "GPU"),
    }


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
    assert "empty line, or Ctrl-C" in message
    assert "no tools" in message
    assert "no access to the filesystem" in message
    assert "separate from Local Code Agent" in message
    assert "Do not guess at feature names, buttons or commands" in message
    assert "OpenVINO Model Server" in message
    assert "NPU" in message


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


def test_quickstart_teaches_the_user_journey_in_the_expected_order():
    text = (REPO / "QUICKSTART.md").read_text(encoding="utf-8")
    install = text.index("## 0. Prepare or check the Windows workstation")
    capabilities = text.index("## 1. See what Local Code Agent can do")
    chat = text.index("## 2. Chat directly with a local model")
    accelerator = text.index("## 3. Prove which accelerator is running Qwen3-8B")
    verification = text.index("## 4. See independent verification reject stale test results")
    assert install < capabilities < chat < accelerator < verification
    assert ".\\install.ps1" in text
    assert ".\\local-code-agent.ps1 capabilities" in text
    assert ".\\chat.ps1 qwen3-8b-npu" in text
    assert "The model can propose actions. It cannot mark its own homework." in text
    assert "qwen3-coder-30b` is a different model on a different profile" in text


def test_previous_research_readme_is_preserved():
    history = (INTERNAL / "docs" / "project-history.md").read_text(encoding="utf-8")
    plain = " ".join(history.replace("**", "").split()).lower()
    assert "measurement instrument first and an agent second" in plain
    assert "| verified completion | 3/10 | 8/10 | 8/10 |" in plain
