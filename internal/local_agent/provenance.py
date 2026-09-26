"""Exact source identity for the product code that produced a result."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_HASHED = (
    ("internal/local_agent", "*.py"),
    ("internal/serving", "*.py"),
    ("internal/scripts", "*.py"),
    ("internal/skills", "*"),
    ("internal/docs/session-contract/v1", "*.json"),
    ("internal/ui", "*"),
)
_HASHED_FILES = (
    "pyproject.toml",
    "local-code-agent.ps1",
    "install.ps1",
    "internal/terminal_ui.py",
)
_SKIP = {"__pycache__", "build", ".local-agent", ".git", ".venv", ".venv-workstation"}


def _key(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _files() -> list[Path]:
    out: list[Path] = []
    for folder, pattern in _HASHED:
        base = _ROOT / folder
        if not base.exists():
            continue
        for path in base.rglob(pattern):
            if not path.is_file() or _SKIP.intersection(path.relative_to(_ROOT).parts):
                continue
            out.append(path)
    for name in _HASHED_FILES:
        path = _ROOT / name
        if path.is_file():
            out.append(path)
    return sorted(set(out), key=_key)


def source_sha256() -> str:
    digest = hashlib.sha256()
    for path in _files():
        digest.update(_key(path).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _from_git() -> dict[str, Any] | None:
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT, capture_output=True, text=True, timeout=10)
        if rev.returncode != 0:
            return None
        status = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT, capture_output=True, text=True, timeout=10)
        return {
            "package_commit": rev.stdout.strip(),
            "package_dirty": bool(status.stdout.strip()),
            "package_source": "git",
        }
    except Exception:
        return None


def package_identity() -> dict[str, Any]:
    stamp = _ROOT / "PACKAGE.json"
    out: dict[str, Any] = {
        "package_commit": None,
        "package_dirty": None,
        "package_source": "unknown",
    }
    if stamp.exists():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            out.update({
                "package_commit": data.get("commit"),
                "package_dirty": data.get("dirty"),
                "package_built_at": data.get("built_at"),
                "package_source": "stamp",
            })
        except Exception:
            pass
    if out["package_commit"] is None:
        out.update(_from_git() or {})
    out["source_sha256"] = source_sha256()
    return out
