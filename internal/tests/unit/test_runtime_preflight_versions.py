from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_preflight_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "runtime-preflight.py"
    spec = importlib.util.spec_from_file_location("lca_runtime_preflight_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "[project]\n"
        "name = \"fixture\"\n"
        "version = \"0.0.0\"\n"
        "dependencies = [\"textual>=0.89,<9\"]\n",
        encoding="utf-8",
    )
    return path


def test_runtime_preflight_rejects_textual_below_declared_minimum(tmp_path, monkeypatch):
    preflight = _load_preflight_module()
    monkeypatch.setattr(preflight.metadata, "version", lambda name: "0.88.1")

    errors = preflight.runtime_errors(_pyproject(tmp_path), critical_imports=())

    assert errors == [
        "declared runtime dependency has incompatible version: textual 0.88.1; required >=0.89,<9"
    ]


def test_runtime_preflight_rejects_textual_above_declared_major(tmp_path, monkeypatch):
    preflight = _load_preflight_module()
    monkeypatch.setattr(preflight.metadata, "version", lambda name: "9.0.0")

    errors = preflight.runtime_errors(_pyproject(tmp_path), critical_imports=())

    assert errors == [
        "declared runtime dependency has incompatible version: textual 9.0.0; required >=0.89,<9"
    ]


def test_runtime_preflight_accepts_supported_textual_version(tmp_path, monkeypatch):
    preflight = _load_preflight_module()
    monkeypatch.setattr(preflight.metadata, "version", lambda name: "8.2.8")

    assert preflight.runtime_errors(_pyproject(tmp_path), critical_imports=()) == []
