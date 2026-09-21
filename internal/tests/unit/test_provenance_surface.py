"""Regression tests for the hashed source surface and package provenance stamp."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INTERNAL = Path(__file__).resolve().parents[2]
REPO = INTERNAL.parent
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from local_agent import provenance  # noqa: E402


def test_every_hashed_folder_exists_and_contributes_files():
    empty = []
    for folder, pattern in provenance._HASHED:
        base = REPO / folder
        if not base.is_dir():
            empty.append(f"{folder} (missing)")
            continue
        if not any(p.is_file() for p in base.rglob(pattern)):
            empty.append(f"{folder}/{pattern} (no matches)")
    assert not empty, f"hashed globs contributing nothing: {empty}"


def test_every_hashed_single_file_exists():
    missing = [name for name in provenance._HASHED_FILES if not (REPO / name).is_file()]
    assert not missing, f"hashed files missing from the tree: {missing}"


def test_pyproject_is_inside_the_hash():
    keys = [provenance._key(p) for p in provenance._files()]
    assert "pyproject.toml" in keys


def test_no_non_python_file_hides_inside_a_python_only_hashed_package():
    patterns: dict[str, set[str]] = {}
    for folder, pattern in provenance._HASHED:
        patterns.setdefault(folder, set()).add(pattern)
    strays = []
    for folder, globs in patterns.items():
        if "*" in globs:
            continue
        base = REPO / folder
        if not base.is_dir():
            continue
        covered = {p for glob in globs for p in base.rglob(glob) if p.is_file()}
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path not in covered:
                strays.append(provenance._key(path))
    assert not strays, (
        "files inside a typed-only hashed folder are invisible to source_sha256: "
        f"{strays}. Move them or widen the hash in the same identity-changing commit."
    )


def test_source_hash_is_stable_and_sorted():
    first = [provenance._key(p) for p in provenance._files()]
    second = [provenance._key(p) for p in provenance._files()]
    assert first == second == sorted(first)
    assert all("\\" not in key for key in first)
    assert provenance.source_sha256() == provenance.source_sha256()


def test_declared_contract_axes_match_the_tree_and_source_is_derived():
    declared = json.loads((INTERNAL / "INSTRUMENT.json").read_text(encoding="utf-8"))
    assert "source_sha256" not in declared
    source = provenance.source_sha256()
    assert len(source) == 64
    assert all(ch in "0123456789abcdef" for ch in source)
    drift = {}
    for key in ("base_prompt_sha256", "outcome_contract_sha256"):
        actual = getattr(provenance, key)()
        if declared.get(key) != actual:
            drift[key] = (declared.get(key), actual)
    assert not drift, f"INSTRUMENT.json frozen-contract drift: {drift}"


def _run_stamper(cwd: Path, path_prepend: str | None = None):
    env = dict(os.environ)
    if path_prepend:
        env["PATH"] = path_prepend + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [sys.executable, str(cwd / "internal" / "scripts" / "stamp_package.py")],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def package_tree(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    shutil.copy2(REPO / "pyproject.toml", dest / "pyproject.toml")
    # stamp_package imports the package before asking Git for status, so the
    # fixture must model the real checkout's ignored interpreter cache. Without
    # this, import-created __pycache__ makes an otherwise clean synthetic repo
    # look dirty for a reason unrelated to the stamper contract.
    (dest / ".gitignore").write_text("__pycache__/\n*.pyc\nPACKAGE.json\n", encoding="utf-8")
    for folder in (
        "internal/local_agent",
        "internal/skills",
        "internal/evaluation",
        "internal/benchmark_fixture/cpp_project",
        "internal/measurement",
        "internal/scripts",
    ):
        src = REPO / folder
        if src.is_dir():
            shutil.copytree(src, dest / folder, ignore=shutil.ignore_patterns("__pycache__", "build"))
    return dest


def test_stamper_writes_commit_dirty_and_matching_source_hash(package_tree):
    subprocess.run(["git", "init", "-q"], cwd=package_tree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=package_tree, check=True)
    subprocess.run(["git", "config", "user.name", "LCA test"], cwd=package_tree, check=True)
    subprocess.run(["git", "add", "."], cwd=package_tree, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=package_tree, check=True)

    proc = _run_stamper(package_tree)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((package_tree / "PACKAGE.json").read_text(encoding="utf-8"))
    assert isinstance(payload["commit"], str) and len(payload["commit"]) >= 7
    assert payload["dirty"] is False

    direct = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0,'internal'); from local_agent.provenance import source_sha256; print(source_sha256())"],
        cwd=package_tree, capture_output=True, text=True, timeout=120,
    )
    assert direct.returncode == 0, direct.stderr
    assert payload["source_sha256"] == direct.stdout.strip()


def test_stamper_marks_dirty_tree(package_tree):
    subprocess.run(["git", "init", "-q"], cwd=package_tree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=package_tree, check=True)
    subprocess.run(["git", "config", "user.name", "LCA test"], cwd=package_tree, check=True)
    subprocess.run(["git", "add", "."], cwd=package_tree, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=package_tree, check=True)
    (package_tree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    proc = _run_stamper(package_tree)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((package_tree / "PACKAGE.json").read_text(encoding="utf-8"))
    assert payload["dirty"] is True


def test_stamper_fails_closed_when_git_cannot_establish_provenance(package_tree):
    fake_bin = package_tree / "nogit"
    fake_bin.mkdir()
    shim = fake_bin / ("git.bat" if os.name == "nt" else "git")
    if os.name == "nt":
        shim.write_text("@exit /b 3\r\n", encoding="utf-8")
    else:
        shim.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        shim.chmod(0o755)

    package_file = package_tree / "PACKAGE.json"
    proc = _run_stamper(package_tree, path_prepend=str(fake_bin))
    assert proc.returncode == 2
    assert "cannot establish Git provenance" in proc.stderr
    assert not package_file.exists(), "unknown provenance must not produce a clean-looking package stamp"
