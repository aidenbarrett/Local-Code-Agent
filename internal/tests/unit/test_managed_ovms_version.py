"""One OVMS version: what setup installs is what every managed-OVMS preset declares."""
from __future__ import annotations

import re
from pathlib import Path

from local_agent.config import MANAGED_OVMS_VERSION, MODEL_PRESETS

REPO = Path(__file__).resolve().parents[3]
SETUP_SCRIPTS = (
    "local-code-agent.ps1",
    "internal/bootstrap-work-laptop-core.ps1",
    "internal/work-laptop-one-shot.ps1",
    "internal/scripts/demo-accelerator.ps1",
)


def test_every_managed_ovms_preset_declares_the_installed_version():
    managed = {name: config for name, config in MODEL_PRESETS.items()
               if config.runtime == "ovms" and not config.serving_experimental}
    assert "nuc-cpu-30b" in managed and "ptl-gpu-30b" in managed
    assert {name: config.runtime_version for name, config in managed.items()} == {
        name: MANAGED_OVMS_VERSION for name in managed}


def test_setup_scripts_install_and_launch_that_same_version():
    for relative in SETUP_SCRIPTS:
        text = (REPO / relative).read_text(encoding="utf-8")
        named = set(re.findall(r"ovms[-_](?:windows_)?(\d+\.\d+\.\d+)", text))
        assert named == {MANAGED_OVMS_VERSION}, (relative, named)
