"""Which model preset a product surface uses. One owner for that decision.

Resolution order: an explicit ``--profile`` for this run, then the user's stored choice
(``local-code-agent.ps1 models use <preset>``), then ``DEFAULT_MODEL_PRESET``. The stored
choice lives in the per-user runtime root, never in a repository.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from local_agent.config import DEFAULT_MODEL_PRESET, MODEL_PRESETS

CHOICE_FILE = "model-preset.json"
LOCAL_RUNTIMES = ("ovms", "llamacpp")


class ModelChoiceError(ValueError):
    """A requested or stored preset cannot be used."""


@dataclass(frozen=True)
class ResolvedPreset:
    name: str
    source: str  # "explicit", "stored" or "default"


def local_presets() -> list[str]:
    """Presets a user can run on this machine without extra configuration."""
    return [name for name, config in MODEL_PRESETS.items() if config.runtime in LOCAL_RUNTIMES]


def choice_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / CHOICE_FILE


def stored_preset(runtime_root: Path) -> str | None:
    """The user's stored choice, or None. A stored name that no longer exists is refused."""
    path = choice_path(runtime_root)
    if not path.is_file():
        return None
    try:
        name = json.loads(path.read_text(encoding="utf-8")).get("preset")
    except (OSError, ValueError, AttributeError) as exc:
        raise ModelChoiceError(
            f"{path} is not a valid model choice; run: .\\local-code-agent.ps1 models use <preset>"
        ) from exc
    if name not in local_presets():
        raise ModelChoiceError(
            f"the stored model preset {name!r} is not available; "
            "run: .\\local-code-agent.ps1 models use <preset>"
        )
    return str(name)


def store_preset(runtime_root: Path, name: str) -> None:
    if name not in local_presets():
        raise ModelChoiceError(
            f"unknown model preset {name!r}; choose one of: {', '.join(local_presets())}"
        )
    path = choice_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"preset": name}) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def resolve_preset(explicit: str | None, runtime_root: Path) -> ResolvedPreset:
    if explicit is not None:
        if explicit not in MODEL_PRESETS:
            raise ModelChoiceError(f"unknown model preset {explicit!r}")
        return ResolvedPreset(explicit, "explicit")
    stored = stored_preset(runtime_root)
    if stored is not None:
        return ResolvedPreset(stored, "stored")
    return ResolvedPreset(DEFAULT_MODEL_PRESET, "default")
