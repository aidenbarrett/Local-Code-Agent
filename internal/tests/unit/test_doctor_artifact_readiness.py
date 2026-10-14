from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from local_agent.repo_setup import inspect_repository, write_proposal
from local_agent.tools.git import observe_output_ignores
from local_agent.tools.tool_primitives import ToolError

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("doctor_artifact_tests", REPO / "internal/scripts/doctor.py")
doctor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = doctor
spec.loader.exec_module(doctor)


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def repository(root, *, build=".local-agent/build", runs=".local-agent/runs"):
    root.mkdir()
    (root / "CMakeLists.txt").write_text("project(example)\n", encoding="utf-8")
    git(root, "init", "-q")
    declaration = write_proposal(inspect_repository(root))
    text = declaration.read_text(encoding="utf-8")
    text = text.replace(".local-agent/build", build).replace(".local-agent/runs", runs)
    declaration.write_text(text, encoding="utf-8")
    return root


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_doctor_names_missing_ignores_without_writing_anything(tmp_path):
    root = repository(tmp_path / "repository")
    before = files(root)
    assert doctor.check_repository(root).state == doctor.READY
    row = doctor.check_artifact_ignores(root, shutil.which)
    assert row.state == doctor.MISSING
    assert "Add-Content -LiteralPath" in row.next_action
    assert "/.local-agent/build/" in row.next_action
    assert "/.local-agent/runs/" in row.next_action
    assert files(root) == before
    assert not (root / ".local-agent").exists()


def test_both_output_directories_must_be_ignored(tmp_path):
    root = repository(tmp_path / "repository")
    (root / ".gitignore").write_text("/.local-agent/build/\n", encoding="utf-8")
    row = doctor.check_artifact_ignores(root, shutil.which)
    assert row.state == doctor.MISSING
    assert "/.local-agent/build/" not in row.next_action
    assert "/.local-agent/runs/" in row.next_action
    (root / ".gitignore").write_text("/.local-agent/\n", encoding="utf-8")
    assert doctor.check_artifact_ignores(root, shutil.which).state == doctor.READY


def test_ignored_but_tracked_output_files_are_not_ready(tmp_path):
    root = repository(tmp_path / "repository")
    (root / ".gitignore").write_text("/.local-agent/\n", encoding="utf-8")
    important = root / ".local-agent/build/important.txt"
    important.parent.mkdir(parents=True)
    important.write_text("user data", encoding="utf-8")
    git(root, "add", "-f", "--", ".local-agent/build/important.txt")
    before = files(root)
    row = doctor.check_artifact_ignores(root, shutil.which)
    assert row.state == doctor.BLOCKED and "tracked files" in row.observed
    assert "Add-Content" not in row.next_action
    assert files(root) == before


def test_unavailable_git_observation_stays_unknown(tmp_path, monkeypatch):
    root = repository(tmp_path / "repository")
    assert doctor.check_artifact_ignores(root, lambda _: None).state == doctor.UNKNOWN

    def unavailable(*args):
        raise ToolError("observation failed")
    monkeypatch.setattr("local_agent.tools.git.observe_output_ignores", unavailable)
    row = doctor.check_artifact_ignores(root, shutil.which)
    assert row.state == doctor.UNKNOWN and row.next_action is None


def test_broken_python_can_still_report_recovery_without_importing_git_tools(tmp_path, monkeypatch):
    root = repository(tmp_path / "repository")
    monkeypatch.setattr(doctor, "check_python", lambda _: doctor.Check(
        "Python", doctor.BLOCKED, observed="missing dependency", next_action="install",
    ))
    def must_not_run(*args):
        raise AssertionError("Git tool observation ran before its runtime was usable")
    monkeypatch.setattr(doctor, "check_artifact_ignores", must_not_run)
    rows = doctor.assess(root, tmp_path / "runtime", pyproject=REPO / "pyproject.toml")
    artifacts = next(row for row in rows if row.name == "Build artifacts")
    assert artifacts.state == doctor.UNKNOWN
    assert "Not ready:" in doctor.render(rows, repo=root)


def test_line_break_or_symlink_never_gets_an_edit_command(tmp_path, monkeypatch):
    root = tmp_path
    row = doctor._artifact_ignore_check(root, "configured", ("bad\nname",), ())
    assert row.state == doctor.BLOCKED and "Add-Content" not in row.next_action
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == ".gitignore")
    row = doctor._artifact_ignore_check(root, "configured", ("build",), ())
    assert row.state == doctor.BLOCKED and "Add-Content" not in row.next_action


@pytest.mark.skipif(sys.platform != "win32", reason="supported PowerShell repair command")
def test_exact_next_action_preserves_existing_rules_and_quotes_literal_paths(tmp_path):
    root = repository(tmp_path / "repo's $literal [x]", build=".local-agent/cache[0]",
                      runs=".local-agent/run logs")
    ignore = root / ".gitignore"
    original = b"keep-rule-without-a-newline"
    ignore.write_bytes(original)
    row = doctor.check_artifact_ignores(root, shutil.which)
    assert row.state == doctor.MISSING
    shell = shutil.which("powershell") or shutil.which("pwsh")
    assert shell
    subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", row.next_action],
                   check=True, capture_output=True)
    assert ignore.read_bytes().startswith(original)
    assert observe_output_ignores(root, (".local-agent/cache[0]", ".local-agent/run logs")).missing == ()
    assert doctor.check_artifact_ignores(root, shutil.which).state == doctor.READY
