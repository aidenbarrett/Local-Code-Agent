from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_installer_next_steps_reference_only_live_product_entrypoints():
    install = (REPO / "install.ps1").read_text(encoding="utf-8")

    assert ".\\chat.ps1" not in install
    assert "  .\\local-code-agent.ps1'" in install
    assert ".\\local-code-agent.ps1 chat qwen3-8b-npu" in install
    assert (REPO / "local-code-agent.ps1").exists()


def test_deleted_chat_wrapper_is_not_a_run_task_dependency():
    presenter = (REPO / "internal" / "scripts" / "run-task-ui.py").read_text(encoding="utf-8")

    assert "ROOT / \"chat.ps1\"" not in presenter
    assert "ROOT / \"local-code-agent.ps1\"" in presenter
