"""A fresh CMake repository: declared with `init`, built and tested from root commands (#459).

The documented path, model-free: `init --write`, then `acceptance --repo <it>
--allow-build --only R02-build --only R03-tests`. Build proof (R02, full_build) and
test proof (R03, full_test with build_target then run_test evidence) are separate
tasks. Tracked and unignored work is unchanged (Git status is identical); the build
creates its normal ignored artifacts in the declared build directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "internal" / "scripts"

pytestmark = pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None or shutil.which("git") is None,
    reason="building a fresh CMake repository needs cmake, ctest and git",
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],  # noqa: S607
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout


def _fresh_cmake_repo(root: Path, *, test_passes: bool) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(fresh CXX)\nenable_testing()\n"
        "add_executable(fresh_test src/fresh_test.cpp)\nadd_test(NAME fresh COMMAND fresh_test)\n",
        encoding="utf-8",
    )
    (root / "src" / "fresh_test.cpp").write_text(
        f"int main() {{ return {0 if test_passes else 1}; }}\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n.local-agent/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fresh")
    return root


def _run(script: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          capture_output=True, text=True, timeout=600, env=env)


def _journeys(tmp_path: Path, repo: Path) -> dict[str, dict]:
    env = {**os.environ, "LCA_RUNTIME_ROOT": str(tmp_path / "runtime"),
           # The child sees exactly this interpreter's import path, as the launcher sets it.
           "PYTHONPATH": os.pathsep.join([str(REPO / "internal"), *sys.path])}
    declared = _run("init-repo.py", "--repo", str(repo / "src"), "--write", env=env)
    assert declared.returncode == 0, declared.stdout + declared.stderr
    _git(repo, "add", ".local-agent.toml")
    _git(repo, "commit", "-qm", "declare")
    before = _git(repo, "status", "--porcelain=v1")
    output = tmp_path / "evidence"
    ran = _run("acceptance-journeys.py", "--output", str(output), "--repo", str(repo),
               "--allow-build", "--only", "R02-build", "--only", "R03-tests", env=env)
    assert _git(repo, "status", "--porcelain=v1") == before, \
        "the journeys changed tracked or unignored work"
    assert (repo / "build").is_dir(), "the declared build directory holds the build's artifacts"
    assert (output / "journeys.json").is_file(), ran.stdout[-3000:] + ran.stderr[-3000:]
    report = json.loads((output / "journeys.json").read_text(encoding="utf-8"))
    journeys = {journey["id"]: journey for journey in report["journeys"]}
    assert "R02-build" in journeys and "R03-tests" in journeys, ran.stdout + ran.stderr
    return journeys


def test_a_fresh_cmake_repository_gets_separate_build_and_test_proof(tmp_path):
    repo = _fresh_cmake_repo(tmp_path / "fresh cmake project", test_passes=True)
    journeys = _journeys(tmp_path, repo)

    build = journeys["R02-build"]
    assert build["status"] == "MEASURED:pass", build
    (build_task,) = build["tasks"]
    assert build_task["verified"] is True
    assert build_task["proof_scope"] == "full_build"
    assert build_task["evidence_ids"] == ["build_target:0"]

    tests = journeys["R03-tests"]
    assert tests["status"] == "MEASURED:pass", tests
    (test_task,) = tests["tasks"]
    assert test_task["verified"] is True
    assert test_task["proof_scope"] == "full_test"
    assert test_task["evidence_ids"] == ["build_target:0", "run_test:1"]
    assert test_task["task_id"] != build_task["task_id"]


def test_a_failing_test_is_measured_as_a_failure_not_proof(tmp_path):
    repo = _fresh_cmake_repo(tmp_path / "fresh cmake project", test_passes=False)
    journeys = _journeys(tmp_path, repo)
    assert journeys["R02-build"]["status"] == "MEASURED:pass"
    tests = journeys["R03-tests"]
    assert tests["status"] == "MEASURED:fail", tests
    (test_task,) = tests["tasks"]
    assert test_task["verified"] is False
    assert test_task["proof_scope"] != "full_test"
