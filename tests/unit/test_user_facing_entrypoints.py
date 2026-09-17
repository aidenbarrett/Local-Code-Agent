from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_chat_module():
    script = ROOT / "scripts" / "chat.py"
    spec = spec_from_file_location("user_chat", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_chat_entrypoint_and_friendly_models_are_present():
    wrapper = (ROOT / "chat.ps1").read_text(encoding="utf-8")
    assert "scripts\\chat.py" in wrapper
    assert "$Args" not in wrapper

    chat = _load_chat_module()
    assert chat.FRIENDLY == {
        "qwen3-8b-npu": ("ptl-npu-8b", "NPU"),
        "qwen3-8b-gpu": ("ptl-npu-8b", "GPU"),
        "qwen3-8b-cpu": ("ptl-npu-8b", "CPU"),
        "qwen3-coder-30b": ("ptl-gpu-30b", "GPU"),
    }


def test_chat_guidance_stays_on_user_facing_entrypoints():
    source = (ROOT / "scripts" / "chat.py").read_text(encoding="utf-8")
    assert ".\\chat.ps1 qwen3-8b-npu" in source
    assert ".\\scripts\\demo-accelerator.ps1 -Device" in source
    assert "-Seconds 5 -KeepServer" in source
    assert "python scripts/chat.py <name>" not in source
    assert "python measurement/serve.py start" not in source
    assert "Available local model choices" in source


def test_local_code_agent_root_facade_explains_why_it_exists():
    wrapper = (ROOT / "local-code-agent.ps1").read_text(encoding="utf-8")
    assert "controlled repository access" in wrapper
    assert "capabilities" in wrapper
    assert "run-task" in wrapper
    assert "verification-demo" in wrapper
    assert "scripts\\capabilities.py" in wrapper
    assert "scripts\\demo-trust-boundary.py" in wrapper


def test_capabilities_use_plain_user_facing_language():
    source = (ROOT / "scripts" / "capabilities.py").read_text(encoding="utf-8")
    assert "Approved tools" in source
    assert "independently check" in source
    assert "typed registry" not in source
    assert "judged by the harness" not in source
    assert ".\\local-code-agent.ps1 verification-demo" in source


def test_quickstart_teaches_the_user_journey_in_the_expected_order():
    text = (ROOT / "QUICKSTART.md").read_text(encoding="utf-8")
    capabilities = text.index("## 1. See what Local Code Agent can do")
    chat = text.index("## 2. Chat directly with a local model")
    accelerator = text.index("## 3. Prove which accelerator is running Qwen3-8B")
    verification = text.index("## 4. See independent verification reject stale test results")
    assert capabilities < chat < accelerator < verification
    assert ".\\local-code-agent.ps1 capabilities" in text
    assert ".\\chat.ps1 qwen3-8b-npu" in text
    assert ".\\local-code-agent.ps1 verification-demo" in text
    assert "-Seconds 5 -KeepServer" in text
    assert "The model can propose actions. It cannot mark its own homework." in text
    assert "qwen3-coder-30b` is a different model on a different profile" in text


def test_previous_research_readme_is_preserved():
    history = (ROOT / "docs" / "project-history.md").read_text(encoding="utf-8")
    assert "measurement instrument first and an agent second" in history
    assert "verified completion** | **3/10** | **8/10** | **8/10**" in history
