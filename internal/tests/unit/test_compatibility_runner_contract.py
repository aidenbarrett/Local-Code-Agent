from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "internal" / "measurement" / "run_test_suite.py"


def _runner():
    spec = spec_from_file_location("compatibility_runner_contract", RUNNER)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_external(test_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUNNER), str(test_file)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_default_test_path_is_the_live_internal_suite():
    runner = _runner()
    assert runner.DEFAULT_TEST_PATH == REPO / "internal" / "tests"
    assert runner.DEFAULT_TEST_PATH.is_dir()


def test_missing_test_path_is_rejected():
    runner = _runner()
    missing = REPO / "tests"
    assert not missing.exists()
    with pytest.raises(ValueError, match="test path does not exist"):
        runner._discover_test_files([missing])


def test_empty_test_directory_is_rejected(tmp_path):
    runner = _runner()
    with pytest.raises(ValueError, match="no test files collected"):
        runner._discover_test_files([tmp_path])


def test_external_test_path_can_pass(tmp_path):
    test_file = tmp_path / "test_external_pass.py"
    test_file.write_text("def test_external_pass():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    result = _run_external(test_file)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 0 failed, 0 skipped" in result.stdout


def test_external_test_path_can_fail_without_runner_crash(tmp_path):
    test_file = tmp_path / "test_external_fail.py"
    test_file.write_text("def test_external_fail():\n    assert False\n", encoding="utf-8")

    result = _run_external(test_file)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "0 passed, 1 failed, 0 skipped" in result.stdout
    assert "ValueError" not in result.stdout + result.stderr
