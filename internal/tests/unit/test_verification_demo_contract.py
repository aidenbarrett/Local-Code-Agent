from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]


def _demo():
    path = INTERNAL / "scripts" / "demo-trust-boundary.py"
    spec = spec_from_file_location("verification_demo_contract", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_direct_test_uses_configured_command_verbatim(monkeypatch, tmp_path):
    demo = _demo()
    calls = []
    command = [
        "ctest",
        "--test-dir",
        "build",
        "-C",
        "Debug",
        "--output-on-failure",
    ]

    class Proc:
        returncode = 0
        stdout = "100% tests passed, 0 tests failed out of 4\n"
        stderr = ""

    def fake_run(argv, *, cwd, capture_output, text, check):
        calls.append((argv, cwd, capture_output, text, check))
        return Proc()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    result = demo.run_tests_directly(tmp_path, command)

    assert result.ok is True
    assert result.summary == "100% tests passed, 0 tests failed out of 4"
    assert calls == [(command, tmp_path, True, True, False)]


def test_direct_test_reports_failed_ctest_as_not_ok(monkeypatch, tmp_path):
    demo = _demo()

    class Proc:
        returncode = 8
        stdout = "0% tests passed, 4 tests failed out of 4\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda *args, **kwargs: Proc(),
    )
    result = demo.run_tests_directly(tmp_path, ["ctest", "-C", "Debug"])

    assert result.ok is False
    assert result.summary == "0% tests passed, 4 tests failed out of 4"


def test_direct_test_rejects_zero_test_green_exit(monkeypatch, tmp_path):
    demo = _demo()

    class Proc:
        returncode = 0
        stdout = "No tests were found!!!\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda *args, **kwargs: Proc(),
    )
    result = demo.run_tests_directly(tmp_path, ["ctest", "-C", "Debug"])

    assert result.ok is False
    assert "no CTest result summary" in result.summary


def test_demo_fails_closed_before_narrating_a_test_pass():
    source = (INTERNAL / "scripts" / "demo-trust-boundary.py").read_text(encoding="utf-8")
    assert "require(direct_honest.ok" in source
    assert "require(direct_stale.ok" in source
    assert "direct_test_command = list(repo.profile().test)" in source
    assert '["ctest", "--test-dir"' not in source
