"""`local-code-agent.ps1 init`: declare a repository without guessing or running anything (#459)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from local_agent.config import load_repo_config
from local_agent.repo_setup import (
    RepoSetupError,
    build_systems,
    init_command,
    inspect_repository,
    propose_cmake,
    validate_declaration,
    write_proposal,
)

REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"


def _script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, INTERNAL / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init = _script("init_repo_under_test", "init-repo.py")


def _cmake_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(demo CXX)\nenable_testing()\n"
        "add_executable(demo_test src/main.cpp)\nadd_test(NAME demo COMMAND demo_test)\n",
        encoding="utf-8",
    )
    (root / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_a_subfolder_of_a_path_with_spaces_opens_the_canonical_root(tmp_path):
    root = _cmake_repo(tmp_path / "my project")
    inspection = inspect_repository(root / "src")
    assert inspection.root == root.resolve()
    assert inspection.opened_from == (root / "src").resolve()
    assert inspection.build_systems == ("CMake",)
    assert inspection.proposal is not None


def test_the_cmake_proposal_uses_the_existing_schema_and_argv(tmp_path):
    proposal = propose_cmake('quote " and back\\slash')
    repo = validate_declaration(proposal)
    profile = repo.profile()
    assert repo.name == 'quote " and back\\slash'
    assert profile.configure == ["cmake", "-S", ".", "-B", "build", "-DCMAKE_BUILD_TYPE=Debug"]
    assert profile.build[:3] == ["cmake", "--build", "build"]
    assert profile.test[0] == "ctest"
    assert repo.policy.allow_patch is False and repo.policy.allow_commit is False


def test_looking_writes_and_runs_nothing(tmp_path, monkeypatch):
    root = _cmake_repo(tmp_path / "repo")
    before = _tree(root)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    assert init.main(["--repo", str(root)]) == init.EXIT_OK
    assert _tree(root) == before


def _forbidden(*_args, **_kwargs):
    raise AssertionError("init ran a process")


def test_write_creates_a_declaration_the_authority_accepts(tmp_path, capsys):
    root = _cmake_repo(tmp_path / "repo")
    assert init.main(["--repo", str(root / "src"), "--write"]) == init.EXIT_OK
    assert "Wrote" in capsys.readouterr().out
    repo = load_repo_config(root)
    assert repo.default_profile == "debug"
    assert repo.name == "repo"


def test_an_existing_declaration_is_never_replaced(tmp_path, capsys):
    root = _cmake_repo(tmp_path / "repo")
    mine = '[profiles.mine]\nbuild = ["make"]\n'
    (root / ".local-agent.toml").write_text(mine, encoding="utf-8")
    assert init.main(["--repo", str(root), "--write"]) == init.EXIT_OK
    out = capsys.readouterr().out
    assert "valid" in out and "Nothing written." in out
    assert (root / ".local-agent.toml").read_text(encoding="utf-8") == mine


def test_a_malformed_declaration_is_reported_and_kept(tmp_path, capsys):
    root = _cmake_repo(tmp_path / "repo")
    broken = "[profiles\n"
    (root / ".local-agent.toml").write_text(broken, encoding="utf-8")
    assert init.main(["--repo", str(root), "--write"]) == init.EXIT_REFUSED
    assert "is invalid" in capsys.readouterr().out
    assert (root / ".local-agent.toml").read_text(encoding="utf-8") == broken


def test_an_outside_root_build_dir_is_refused_by_the_authority(tmp_path):
    with pytest.raises(RepoSetupError, match="invalid"):
        validate_declaration(
            '[repo]\nbuild_dir = "../outside"\n[profiles.debug]\nbuild = ["cmake"]\n'
        )


def test_a_race_never_overwrites_a_declaration_written_after_inspection(tmp_path):
    root = _cmake_repo(tmp_path / "repo")
    inspection = inspect_repository(root)
    (root / ".local-agent.toml").write_text("# the user's\n", encoding="utf-8")
    with pytest.raises(RepoSetupError, match="already exists"):
        write_proposal(inspection)
    assert (root / ".local-agent.toml").read_text(encoding="utf-8") == "# the user's\n"


@pytest.mark.parametrize(("markers", "message"), [
    (("CMakeLists.txt", "Makefile"), "more than one build system"),
    (("Cargo.toml",), "CMake/CTest declarations only"),
    (("demo.sln",), "MSBuild"),
    ((), "no recognised build system"),
])
def test_ambiguous_unsupported_or_absent_build_systems_are_refused_before_writing(
        tmp_path, markers, message):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for marker in markers:
        (root / marker).write_text("", encoding="utf-8")
    before = _tree(root)
    assert init.main(["--repo", str(root), "--write"]) == init.EXIT_REFUSED
    inspection = inspect_repository(root)
    assert inspection.proposal is None and inspection.refusal is not None
    assert message in inspection.refusal
    assert _tree(root) == before


def test_a_marker_directory_is_not_a_build_system(tmp_path):
    root = tmp_path / "repo"
    (root / "Makefile").mkdir(parents=True)
    (root / "CMakeLists.txt").write_text("", encoding="utf-8")
    assert build_systems(root) == ("CMake",)


def test_a_missing_directory_is_refused(tmp_path, capsys):
    assert init.main(["--repo", str(tmp_path / "absent")]) == init.EXIT_REFUSED
    assert "is not a directory" in capsys.readouterr().err


def test_the_session_hub_refuses_an_undeclared_repository_with_the_init_step(
        tmp_path, capsys, monkeypatch):
    session_hub = _script("session_hub_for_init_test", "session-hub.py")
    root = _cmake_repo(tmp_path / "my project")
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("LCA_RUNTIME_ROOT", str(runtime))
    assert session_hub.main(["--repo", str(root / "src"), "--check"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Repository not ready" in err
    assert f"Next: {init_command(root.resolve())}" in err
    assert not runtime.exists(), "the refusal came after a runtime effect"


def test_the_session_hub_names_a_malformed_declaration(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LCA_RUNTIME_ROOT", str(tmp_path / "runtime"))
    session_hub = _script("session_hub_for_init_test", "session-hub.py")
    root = _cmake_repo(tmp_path / "repo")
    (root / ".local-agent.toml").write_text("[profiles\n", encoding="utf-8")
    assert session_hub.main(["--repo", str(root), "--check"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert f"Next: correct {root.resolve() / '.local-agent.toml'}" in err


def test_help_names_init():
    help_text = (INTERNAL / "scripts" / "product-help.py").read_text(encoding="utf-8")
    assert 'term.field("init"' in help_text


@pytest.mark.skipif(os.name != "nt", reason="the public launcher is Windows PowerShell")
def test_the_public_launcher_declares_a_repository_in_a_path_with_spaces(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell, "a Windows CI runner must provide PowerShell"
    root = _cmake_repo(tmp_path / "my project")
    base = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(REPO / "local-code-agent.ps1"), "init"]
    looked = subprocess.run(base, capture_output=True, text=True, timeout=180, cwd=root / "src")
    assert looked.returncode == 0, looked.stdout + looked.stderr
    assert f"Repository root: {root.resolve()}" in looked.stdout
    assert not (root / ".local-agent.toml").exists()
    wrote = subprocess.run([*base, "--write"], capture_output=True, text=True, timeout=180,
                           cwd=root / "src")
    assert wrote.returncode == 0, wrote.stdout + wrote.stderr
    assert load_repo_config(root).default_profile == "debug"
