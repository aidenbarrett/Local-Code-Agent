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


def test_direct_test_uses_configured_command_and_discovers_same_suite(monkeypatch, tmp_path):
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

    class RunProc:
        returncode = 0
        stdout = "100% tests passed, 0 tests failed out of 4\n"
        stderr = ""

    class ListProc:
        returncode = 0
        stdout = "Total Tests: 4\n"
        stderr = ""

    def fake_run(argv, *, cwd, capture_output, text, check):
        calls.append((argv, cwd, capture_output, text, check))
        return ListProc() if argv[-1] == "-N" else RunProc()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    result = demo.run_tests_directly(tmp_path, command)

    assert result.ok is True
    assert result.summary == "100% tests passed, 0 tests failed out of 4"
    assert calls == [
        (command, tmp_path, True, True, False),
        ([*command, "-N"], tmp_path, True, True, False),
    ]


def test_direct_test_accepts_green_ctest_without_standard_summary_when_suite_is_nonempty(monkeypatch, tmp_path):
    demo = _demo()
    command = ["ctest", "--test-dir", "build", "-C", "Debug", "--output-on-failure"]

    class RunProc:
        returncode = 0
        stdout = "All requested test processes completed successfully.\n"
        stderr = ""

    class ListProc:
        returncode = 0
        stdout = "Test project C:/tmp/build\n  Test #1: a\n  Test #2: b\nTotal Tests: 2\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda argv, **kwargs: ListProc() if argv[-1] == "-N" else RunProc(),
    )
    result = demo.run_tests_directly(tmp_path, command)

    assert result.ok is True
    assert result.summary == "configured CTest command passed; 2 test(s) discovered"


def test_direct_test_reports_failed_ctest_as_not_ok(monkeypatch, tmp_path):
    demo = _demo()

    class RunProc:
        returncode = 8
        stdout = "0% tests passed, 4 tests failed out of 4\n"
        stderr = ""

    class ListProc:
        returncode = 0
        stdout = "Total Tests: 4\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda argv, **kwargs: ListProc() if argv[-1] == "-N" else RunProc(),
    )
    result = demo.run_tests_directly(tmp_path, ["ctest", "-C", "Debug"])

    assert result.ok is False
    assert result.summary == "0% tests passed, 4 tests failed out of 4"


def test_direct_test_rejects_zero_test_green_exit(monkeypatch, tmp_path):
    demo = _demo()

    class RunProc:
        returncode = 0
        stdout = "No tests were found!!!\n"
        stderr = ""

    class ListProc:
        returncode = 0
        stdout = "Total Tests: 0\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda argv, **kwargs: ListProc() if argv[-1] == "-N" else RunProc(),
    )
    result = demo.run_tests_directly(tmp_path, ["ctest", "-C", "Debug"])

    assert result.ok is False
    assert "CTest discovered 0 tests" in result.summary


def test_direct_test_rejects_unverifiable_discovery(monkeypatch, tmp_path):
    demo = _demo()

    class RunProc:
        returncode = 0
        stdout = "custom successful output\n"
        stderr = ""

    class ListProc:
        returncode = 0
        stdout = "CTest listed tests but omitted its total\n"
        stderr = ""

    monkeypatch.setattr(
        demo.subprocess,
        "run",
        lambda argv, **kwargs: ListProc() if argv[-1] == "-N" else RunProc(),
    )
    result = demo.run_tests_directly(tmp_path, ["ctest", "-C", "Debug"])

    assert result.ok is False
    assert "test discovery could not be verified" in result.summary


def test_demo_fails_closed_before_narrating_a_test_pass():
    source = (INTERNAL / "scripts" / "demo-trust-boundary.py").read_text(encoding="utf-8")
    assert "require(direct_honest.ok" in source
    assert "require(direct_stale.ok" in source
    assert "direct_test_command = list(repo.profile().test)" in source
    assert "_ctest_discovered_count" in source
