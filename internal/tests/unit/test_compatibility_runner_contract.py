from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "internal" / "measurement" / "run_test_suite.py"


def _runner():
    spec = spec_from_file_location("compatibility_runner_contract", RUNNER)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
