"""Where runtime state and downloaded model weights live. One owner for the layout."""
from __future__ import annotations

import os
from pathlib import Path


def default_runtime_root() -> Path:
    """The default location for runtime state and downloaded model weights."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LocalCodeAgent"


def model_repository(runtime_root: Path) -> Path:
    """Where every model's weights live under a runtime root: <root>/models/<org>/<name>."""
    return Path(runtime_root).absolute() / "models"
