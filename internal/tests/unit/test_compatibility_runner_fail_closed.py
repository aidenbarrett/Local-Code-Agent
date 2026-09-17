from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "internal" / "measurement" / "run_test_suite.py"


def _run(*args: str):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_compatibility_runner_default_points_at_live_test_tree():
    source = RUNNER.read_text(encoding="utf-8")
    assert 'DEFAULT_TEST_PATH = Path(__file__).resolve().parents[1] / "tests"' in source
    assert 'paths = [Path(p) for p in args.paths] if args.paths else [DEFAULT_TEST_PATH]' in source


def test_compatibility_runner_refuses_missing_test_path(tmp_path):
    result = _run(str(tmp_path / "missing"))
    assert result.returncode == 2
    assert "test path does not exist" in result.stderr


def test_compatibility_runner_refuses_empty_test_directory(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(str(empty))
    assert result.returncode == 2
    assert "no test files collected" in result.stderr
