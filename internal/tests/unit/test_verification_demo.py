from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[3]
INTERNAL = REPO / "internal"
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))


def _load_demo_module():
    script = INTERNAL / "scripts" / "demo-trust-boundary.py"
    spec = spec_from_file_location("verification_demo", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_default_test_command_is_windows_multi_config_safe():
    from local_agent.config import load_repo_config

    fixture = INTERNAL / "benchmark_fixture" / "cpp_project"
    command = load_repo_config(fixture).profile().test
    assert command == [
        "ctest",
        "--test-dir",
        "build",
        "-C",
        "Debug",
        "--output-on-failure",
    ]


def test_direct_test_probe_runs_the_exact_configured_command(monkeypatch, tmp_path):
    demo = _load_demo_module()
    expected = [
        "ctest",
        "--test-dir",
        "build",
        "-C",
        "Debug",
        "--output-on-failure",
    ]
    seen = {}

    class Result:
        returncode = 0
        stdout = "100% tests passed, 0 tests failed out of 4\n"
        stderr = ""

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return Result()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    code, summary = demo.ctest_directly(tmp_path, expected)

    assert seen["command"] == expected
    assert seen["cwd"] == tmp_path
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["check"] is False
    assert code == 0
    assert summary == "100% tests passed, 0 tests failed out of 4"


def test_demo_does_not_hardcode_a_platform_specific_ctest_invocation():
    source = (INTERNAL / "scripts" / "demo-trust-boundary.py").read_text(encoding="utf-8")
    assert "test_command = list(profile.test)" in source
    assert source.count("ctest_directly(root, test_command)") == 2
    assert '["ctest", "--test-dir"' not in source
    assert "direct_code == 0" in source
    assert "stale_code == 0" in source
